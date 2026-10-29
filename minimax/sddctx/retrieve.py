"""检索：BM25（含结构化加权），候选召回与统计量全部来自数据库查询。

流程（每一步都对应一条可在数据库里复跑的 SQL，见 ``trace``）：

1. 任务文本分词 → 检索词集合；
2. ``SELECT term, df FROM pc_df WHERE term IN (...)`` 取文档频率；
3. ``SELECT ... FROM pc_postings JOIN pc_chunks ...`` 取每个候选块的词频与长度；
4. 应用层按 BM25 公式累加（k1=1.2, b=0.6），并对「命中代码符号 / 文件路径」的块加权；
5. 取 Top-K 反向查 ``pc_chunks`` 拿正文。

BM25 的累加放在应用层而不是 SQL 里，是为了避免依赖数据库的 LOG/浮点实现差异；
候选集、词频、文档频率、块长这些「事实数据」都来自数据库，检索结果不是凭空造的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from sddctx.db import Database
from sddctx.textutil import tokenize

K1 = 1.2
B = 0.6
MAX_QUERY_TERMS = 96


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    category: str
    path: str
    symbol: str
    heading: str
    start_line: int
    end_line: int
    content: str
    score: float
    matched_terms: list[str] = field(default_factory=list)
    spec_version: str = ""

    def to_dict(self, excerpt_chars: int | None = None) -> dict[str, Any]:
        content = self.content
        if excerpt_chars and len(content) > excerpt_chars:
            content = content[:excerpt_chars].rstrip() + "\n…"
        return {
            "source_id": self.symbol,
            "category": self.category,
            "path": self.path,
            "symbol": self.symbol,
            "heading": self.heading,
            "lines": [self.start_line, self.end_line],
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms[:12],
            "spec_version": self.spec_version,
            "content": content,
        }


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def search(
    db: Database,
    query: str,
    *,
    category: str | None = None,
    limit: int = 8,
    excerpt_chars: int | None = None,
) -> tuple[list[Hit], dict[str, Any]]:
    """检索并返回 (hits, trace)。``trace`` 记录本次检索用到的 SQL 与行数。"""
    terms = [term for term in dict.fromkeys(tokenize(query))][:MAX_QUERY_TERMS]
    trace: dict[str, Any] = {"query": query, "category": category, "terms": len(terms), "steps": []}
    if not terms:
        return [], trace

    marks = _placeholders(len(terms))
    df_rows = db.query(f"SELECT term, df FROM pc_df WHERE term IN ({marks})", terms)
    trace["steps"].append({"step": "df", "rows": len(df_rows)})
    if not df_rows:
        return [], trace

    total_chunks = int(db.scalar("SELECT COUNT(*) FROM pc_chunks", default=0)) or 0
    avg_len = float(db.scalar("SELECT AVG(token_len) FROM pc_chunks", default=0.0) or 0.0) or 1.0
    trace["corpus"] = {"chunks": total_chunks, "avg_token_len": round(avg_len, 2)}

    params: list[Any] = list(terms)
    sql = (
        "SELECT p.chunk_id, p.term, p.tf, c.token_len, c.symbol, c.path "
        "FROM pc_postings p JOIN pc_chunks c ON c.chunk_id = p.chunk_id "
        f"WHERE p.term IN ({marks})"
    )
    if category:
        sql += " AND c.category = ?"
        params.append(category)
    posting_rows = db.query(sql, params)
    trace["steps"].append({"step": "postings", "rows": len(posting_rows)})

    idf = {row["term"]: math.log(1 + (total_chunks - row["df"] + 0.5) / (row["df"] + 0.5)) for row in df_rows}
    scores: dict[str, float] = {}
    matched: dict[str, set[str]] = {}
    meta: dict[str, dict[str, str]] = {}
    for row in posting_rows:
        term = row["term"]
        if term not in idf:
            continue
        tf = float(row["tf"])
        length = float(row["token_len"]) or 1.0
        denom = tf + K1 * (1 - B + B * length / avg_len)
        contribution = idf[term] * (tf * (K1 + 1)) / denom
        chunk_id = row["chunk_id"]
        scores[chunk_id] = scores.get(chunk_id, 0.0) + contribution
        matched.setdefault(chunk_id, set()).add(term)
        meta[chunk_id] = {"symbol": row["symbol"], "path": row["path"]}

    query_lower = query.lower()
    for chunk_id, value in list(scores.items()):
        symbol_lower = meta[chunk_id]["symbol"].lower()
        path_lower = meta[chunk_id]["path"].lower()
        boosted = value
        if any(term in symbol_lower for term in matched[chunk_id]):
            boosted *= 1.25
        if path_lower and path_lower.rsplit("/", 1)[-1].replace(".py", "").replace(".md", "") in query_lower:
            boosted *= 1.15
        scores[chunk_id] = boosted

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[: max(limit * 3, limit)]
    if not ordered:
        return [], trace

    ids = [chunk_id for chunk_id, _ in ordered]
    marks = _placeholders(len(ids))
    rows = db.query(
        "SELECT chunk_id, doc_id, category, path, symbol, heading, start_line, end_line, content "
        f"FROM pc_chunks WHERE chunk_id IN ({marks})",
        ids,
    )
    trace["steps"].append({"step": "chunks", "rows": len(rows)})
    doc_ids = list({row["doc_id"] for row in rows})
    version_rows = db.query(
        f"SELECT doc_id, spec_version FROM pc_documents WHERE doc_id IN ({_placeholders(len(doc_ids))})",
        doc_ids,
    )
    trace["steps"].append({"step": "documents", "rows": len(version_rows)})
    versions = {row["doc_id"]: row["spec_version"] for row in version_rows}

    by_id = {row["chunk_id"]: row for row in rows}
    hits: list[Hit] = []
    for chunk_id, score in ordered[:limit]:
        row = by_id.get(chunk_id)
        if not row:
            continue
        hits.append(
            Hit(
                chunk_id=chunk_id,
                doc_id=row["doc_id"],
                category=row["category"],
                path=row["path"],
                symbol=row["symbol"],
                heading=row["heading"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                content=row["content"],
                score=score,
                matched_terms=sorted(matched.get(chunk_id, set())),
                spec_version=versions.get(row["doc_id"], ""),
            )
        )
    trace["hits"] = [{"source_id": hit.symbol, "score": round(hit.score, 4)} for hit in hits]
    return hits, trace


def search_all(
    db: Database,
    query: str,
    limits: dict[str, int],
    *,
    excerpt_chars: int | None = None,
) -> tuple[dict[str, list[Hit]], dict[str, Any]]:
    """按类别分别检索，返回 {category: [hit]} 与合并后的 trace。"""
    buckets: dict[str, list[Hit]] = {}
    traces: dict[str, Any] = {}
    for category, limit in limits.items():
        hits, trace = search(db, query, category=category, limit=limit, excerpt_chars=excerpt_chars)
        buckets[category] = hits
        traces[category] = trace
    return buckets, traces
