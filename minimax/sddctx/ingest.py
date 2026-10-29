"""资料导入：规格 / 代码 / 测试 → 分块 → 倒排索引落库。

导入是幂等的、增量的：

* ``doc_id`` 由「类别 + 定位键」的哈希决定（规格用条目号，代码/测试用相对路径），
  同一份资料重复导入只会覆盖旧块；
* ``content_sha`` 未变化的文档直接跳过，只重建变更文档的倒排；
* 变更文档的旧块用批量 ``DELETE ... WHERE doc_id IN (...)`` 清理，
  避免逐文档删除（seekdb 单条 DELETE 在秒级，批量后整个导入降到 2 秒量级）。

规格更新后重新导入，旧块被替换，检索自然命中新内容——这是「输出不是固定答案」的基础。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sddctx import chunking
from sddctx.chunking import Document
from sddctx.config import Settings
from sddctx.db import Database
from sddctx.textutil import now_iso, short_id, spec_refs, term_counts

CATEGORY_LABELS = {"spec": "规格", "code": "代码", "test": "测试"}
DELETE_BATCH = 200


@dataclass
class IngestReport:
    backend: str
    spec_variant: str
    documents: int
    chunks: int
    postings: int
    terms: int
    categories: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "spec_variant": self.spec_variant,
            "documents": self.documents,
            "chunks": self.chunks,
            "postings": self.postings,
            "terms": self.terms,
            "categories": self.categories,
            "files": self.files,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "elapsed_ms": self.elapsed_ms,
        }


def _spec_files(settings: Settings, spec_variant: str) -> list[Path]:
    """选出生效的规格文件；``v2`` 时用 variants/ 下的新版本覆盖同名规格。"""
    selected: dict[str, Path] = {}
    for path in sorted(settings.specs_dir.glob("SPEC-*.md")):
        selected[path.name.split("-")[1]] = path
    if spec_variant and spec_variant != "v1":
        for path in sorted((settings.specs_dir / "variants").glob(f"*.{spec_variant}.md")):
            selected[path.name.split("-")[1]] = path
    return [selected[key] for key in sorted(selected)]


def collect_documents(settings: Settings, spec_variant: str = "v1") -> list[Document]:
    documents: list[Document] = []
    for path in _spec_files(settings, spec_variant):
        rel = path.relative_to(settings.root).as_posix()
        documents.append(chunking.chunk_markdown(path, rel))

    for path in sorted(settings.demo_project.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(settings.root).as_posix()
        category = "test" if "tests" in path.parts else "code"
        documents.append(chunking.chunk_python(path, rel, category))
    return documents


def _doc_key(document: Document) -> str:
    """规格按「条目号」定位，其余按相对路径定位。

    规格有多个版本（specs/ 与 specs/variants/），同一份规格的不同版本必须共用
    一个 doc_id，重新导入才会「替换」而不是「并存」，否则检索会同时命中新旧条款。
    """
    if document.category == "spec":
        parts = Path(document.path).name.split("-")
        if len(parts) > 1:
            return f"SPEC-{parts[1]}"
    return document.path


def _doc_id(document: Document) -> str:
    return "d" + short_id(document.category, _doc_key(document))


def _chunk_rows(document: Document, doc_id: str) -> tuple[list[tuple], list[tuple]]:
    chunk_rows: list[tuple] = []
    posting_rows: list[tuple] = []
    for chunk in document.chunks:
        chunk_id = "c" + short_id(doc_id, chunk.symbol, str(chunk.start_line))
        index_text = f"{document.path}\n{chunk.symbol}\n{chunk.heading}\n{chunk.content}"
        counts = term_counts(index_text)
        # 规格条目号在文中出现一次，按 3 次计入：它是跨文档的精确锚点，应强于普通词
        for ref in spec_refs(index_text):
            counts[ref] += 3
        chunk_rows.append(
            (
                chunk_id,
                doc_id,
                document.category,
                document.path,
                chunk.symbol,
                chunk.heading,
                chunk.start_line,
                chunk.end_line,
                sum(counts.values()),
                chunk.content,
            )
        )
        posting_rows.extend((term, chunk_id, freq) for term, freq in counts.items())
    return chunk_rows, posting_rows


def _batched(values: list[str], size: int = DELETE_BATCH) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _purge_all(db: Database) -> None:
    """全量重建前的清空：整表删除，顺带清掉历史中断留下的孤儿倒排行。"""
    db.execute("DELETE FROM pc_postings")
    db.execute("DELETE FROM pc_chunks")
    db.execute("DELETE FROM pc_documents")


def _purge(db: Database, doc_ids: list[str]) -> None:
    for batch in _batched(doc_ids):
        marks = ", ".join("?" for _ in batch)
        db.execute(
            f"DELETE FROM pc_postings WHERE chunk_id IN (SELECT chunk_id FROM pc_chunks WHERE doc_id IN ({marks}))",
            batch,
        )
        db.execute(f"DELETE FROM pc_chunks WHERE doc_id IN ({marks})", batch)
        db.execute(f"DELETE FROM pc_documents WHERE doc_id IN ({marks})", batch)


def ingest(settings: Settings, db: Database, spec_variant: str = "v1", reset: bool = False) -> IngestReport:
    started = time.time()
    # reset = 清空数据重建索引（保留表结构）；建表只在缺表时发生
    db.ensure_schema(reset=reset)
    documents = collect_documents(settings, spec_variant)

    known: dict[str, str] = {}
    if not reset:
        for row in db.query("SELECT doc_id, content_sha FROM pc_documents"):
            known[row["doc_id"]] = row["content_sha"]

    changed: list[Document] = documents if reset else [
        document for document in documents if known.get(_doc_id(document)) != document.content_sha
    ]
    unchanged = len(documents) - len(changed)

    if reset:
        _purge_all(db)
    elif changed:
        _purge(db, [_doc_id(document) for document in changed])

    categories: Counter[str] = Counter()
    for document in documents:
        categories[document.category] += 1

    total_chunks = 0
    total_postings = 0
    for document in changed:
        doc_id = _doc_id(document)
        db.execute(
            "INSERT INTO pc_documents (doc_id, category, path, title, spec_version, content_sha, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                document.category,
                document.path,
                document.title,
                document.spec_version,
                document.content_sha,
                now_iso(),
            ),
        )
        chunk_rows, posting_rows = _chunk_rows(document, doc_id)
        db.executemany(
            "INSERT INTO pc_chunks (chunk_id, doc_id, category, path, symbol, heading, start_line, end_line, token_len, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            chunk_rows,
        )
        db.executemany("INSERT INTO pc_postings (term, chunk_id, tf) VALUES (?, ?, ?)", posting_rows)
        total_chunks += len(chunk_rows)
        total_postings += len(posting_rows)

    if changed:
        db.execute("DELETE FROM pc_df")
        db.execute("INSERT INTO pc_df (term, df) SELECT term, COUNT(*) FROM pc_postings GROUP BY term")

    report = IngestReport(
        backend=db.backend,
        spec_variant=spec_variant,
        documents=len(documents),
        chunks=total_chunks,
        postings=total_postings,
        terms=int(db.scalar("SELECT COUNT(*) FROM pc_df")),
        categories=dict(categories),
        files=[document.path for document in documents],
        changed=[document.path for document in changed],
        unchanged=unchanged,
        elapsed_ms=int((time.time() - started) * 1000),
    )
    db.put_state("last_ingest", json.dumps(report.to_dict(), ensure_ascii=False))
    return report
