"""上下文组装：把「任务 → 规格条目 → 代码符号 → 测试」拼成一份可交付的上下文包。

一份上下文包同时服务于三类读者：

* 人（评审）：``context.md`` 里有排序、分数、来源行号，可逐条核对；
* 开发助手（MiniMax）：``prompt`` 字段是裁剪过的纯文本，直接塞进对话；
* 后续运行：``runs/<run_id>/context.json`` + PowerContext 里的 ``context_built`` 事件。

``checks`` 字段是给验收用的：任务文件里声明了「本次上下文必须包含哪些来源」，
这里逐条核对是否命中，评审可以一条命令确认上下文正确性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sddctx.config import Settings
from sddctx.db import Database
from sddctx.retrieve import Hit, search_all
from sddctx.state import StateStore, task_state_view
from sddctx.textutil import now_iso

# 每类的保留条数：规格按小节切（4 条覆盖到相关条款与相邻条款）；代码按符号切（5 条覆盖
# 待改函数与其依赖）；测试保留 7 条，正好覆盖目标测试文件的 6 个用例 + 文件级说明，
# 避免因为排序抖动漏掉验收用例。
DEFAULT_LIMITS = {"spec": 4, "code": 5, "test": 7}
EXCERPTS = {"spec": 1800, "code": 3000, "test": 1800}


class TaskNotFound(RuntimeError):
    pass


def load_task(settings: Settings, task_id: str) -> dict[str, Any]:
    path = settings.task_path(task_id)
    if not path.is_file():
        raise TaskNotFound(f"任务文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_query(task: dict[str, Any]) -> str:
    parts = [task.get("title", ""), task.get("summary", "")]
    parts.extend(task.get("query_hints", []))
    parts.extend(Path(path).name for path in task.get("target_tests", []))
    parts.extend(Path(path).name for path in task.get("regression_tests", []))
    return "\n".join(part for part in parts if part)


@dataclass
class ContextPack:
    payload: dict[str, Any]
    markdown: str
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return self.payload


def _hit_dict(hit: Hit, excerpt_chars: int, why: str) -> dict[str, Any]:
    data = hit.to_dict(excerpt_chars=excerpt_chars)
    data["why"] = why
    return data


def _why_spec(hit: Hit) -> str:
    return f"命中任务关键词 {', '.join(hit.matched_terms[:4])}；条款 {hit.symbol} 直接约束本次改动"


def _why_code(hit: Hit) -> str:
    return f"符号 {hit.symbol} 是本次需要修改的位置（命中的词：{', '.join(hit.matched_terms[:4])}）"


def _why_test(hit: Hit) -> str:
    return f"该用例覆盖任务要求的可验证行为（命中的词：{', '.join(hit.matched_terms[:4])}）"


def build_context(
    settings: Settings,
    db: Database,
    store: StateStore,
    task: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    state_fallback_reason: str = "",
    persist: bool = True,
) -> ContextPack:
    limits = limits or dict(DEFAULT_LIMITS)
    query = build_query(task)
    buckets, traces = search_all(db, query, limits, excerpt_chars=None)

    ingested = db.get_state("last_ingest")
    last_ingest = json.loads(ingested) if ingested else {}
    spec_variant = last_ingest.get("spec_variant", settings.spec_variant)

    state_view = task_state_view(store, task, state_fallback_reason)
    run_id = f"run-{now_iso().replace(':', '').replace('-', '')[0:15]}-{task['task_id']}"
    generated_at = now_iso()

    sources = {
        "spec": [
            _hit_dict(hit, EXCERPTS["spec"], _why_spec(hit)) for hit in buckets.get("spec", [])
        ],
        "code": [
            _hit_dict(hit, EXCERPTS["code"], _why_code(hit)) for hit in buckets.get("code", [])
        ],
        "test": [
            _hit_dict(hit, EXCERPTS["test"], _why_test(hit)) for hit in buckets.get("test", [])
        ],
    }

    checks = check_required_sources(task, sources, spec_variant)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task["task_id"],
        "task_title": task.get("title", ""),
        "task_summary": task.get("summary", ""),
        "generated_at": generated_at,
        "spec_variant": spec_variant,
        "storage": {
            "backend": db.backend,
            "label": db.label,
            "database": db.database,
            "fallback_reason": db.fallback_reason,
        },
        "query": {
            "text": query,
            "terms": traces.get("spec", {}).get("terms", 0),
            "traces": traces,
        },
        "sources": sources,
        "state": state_view,
        "steps": task.get("steps", []),
        "target_tests": task.get("target_tests", []),
        "regression_tests": task.get("regression_tests", []),
        "checks": checks,
    }
    payload["prompt"] = render_prompt(payload)
    markdown = render_markdown(payload)

    if persist:
        run_dir = settings.data_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "context.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "context.md").write_text(markdown, encoding="utf-8")
        db.execute("DELETE FROM pc_runs WHERE run_id = ?", (run_id,))
        db.execute(
            "INSERT INTO pc_runs (run_id, task_id, created_at, backend, payload) VALUES (?, ?, ?, ?, ?)",
            (run_id, task["task_id"], generated_at, db.backend, json.dumps(payload, ensure_ascii=False)),
        )
        store.append(
            task["task_id"],
            "context_built",
            run_id=run_id,
            spec_variant=spec_variant,
            sources={
                "spec": [hit["source_id"] for hit in sources["spec"]],
                "code": [hit["source_id"] for hit in sources["code"]],
                "test": [hit["source_id"] for hit in sources["test"]],
            },
            backend=db.backend,
        )
        payload["run_dir"] = str(run_dir)
    return ContextPack(payload=payload, markdown=markdown, prompt=payload["prompt"])


def check_required_sources(
    task: dict[str, Any],
    sources: dict[str, list[dict[str, Any]]],
    spec_variant: str,
) -> dict[str, Any]:
    required = task.get("required_sources", {})
    present = {
        category: [item["source_id"] for item in sources.get(category, [])] for category in ("spec", "code", "test")
    }
    missing: dict[str, list[str]] = {}
    for category, ids in required.items():
        absent = [required_id for required_id in ids if required_id not in present.get(category, [])]
        if absent:
            missing[category] = absent

    marker_checks: list[dict[str, Any]] = []
    for marker in task.get("required_markers", []):
        variants = marker.get("variants") or []
        if variants and spec_variant not in variants:
            continue
        hit = next(
            (
                item
                for category in ("spec", "code", "test")
                for item in sources.get(category, [])
                if marker["text"] in item["content"] and marker.get("source_id", "") in ("", item["source_id"])
            ),
            None,
        )
        marker_checks.append(
            {
                "text": marker["text"],
                "expected_source": marker.get("source_id", ""),
                "found": hit is not None,
                "actual_source": hit["source_id"] if hit else "",
            }
        )

    return {
        "spec_variant": spec_variant,
        "required": required,
        "present": present,
        "missing": missing,
        "markers": marker_checks,
        "all_required_present": not missing and all(item["found"] for item in marker_checks),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 开发上下文 · {payload['task_id']} {payload['task_title']}")
    add("")
    add(f"- 生成时间：{payload['generated_at']}")
    add(f"- 上下文编号：`{payload['run_id']}`")
    add(f"- 规格版本：`{payload['spec_variant']}`")
    storage = payload["storage"]
    add(f"- 资料库：`{storage['backend']}`（{storage['label']}，库 `{storage['database']}`）")
    if storage.get("fallback_reason"):
        add(f"- 降级原因：{storage['fallback_reason']}")
    add("")
    add("## 任务")
    add("")
    add(payload["task_summary"])
    add("")

    titles = {"spec": "适用规格条目", "code": "需要修改的代码", "test": "验证用测试"}
    for category in ("spec", "code", "test"):
        items = payload["sources"].get(category, [])
        add(f"## {titles[category]}（{len(items)}）")
        add("")
        if not items:
            add("_未检索到相关内容_")
            add("")
            continue
        for item in items:
            add(f"### `{item['source_id']}`")
            add("")
            add(f"- 文件：`{item['path']}` 第 {item['lines'][0]}–{item['lines'][1]} 行")
            add(f"- 相关度：{item['score']}（命中词：{', '.join(item['matched_terms'][:6]) or '—'}）")
            add(f"- 入选理由：{item['why']}")
            if item.get("spec_version"):
                add(f"- 规格版本：`{item['spec_version']}`")
            add("")
            add("```")
            add(item["content"].rstrip())
            add("```")
            add("")

    add("## 任务状态（PowerContext）")
    add("")
    state = payload["state"]
    add(f"- 状态后端：`{state['backend']}`（scope: `{state['scope']}`）")
    if state.get("fallback_reason"):
        add(f"- 降级原因：{state['fallback_reason']}")
    add(f"- 已完成：{'；'.join(state['completed']) or '无'}")
    add(f"- 待办：{'；'.join(state['pending']) or '无'}")
    if state["verifications"]:
        add("- 已有验证结果：")
        for item in state["verifications"]:
            add(
                f"  - {item['phase']}：exit={item['exit_code']}，passed={item['passed']}，"
                f"`{item['command']}`（{item['at']}）"
            )
    else:
        add("- 已有验证结果：无")
    add("")

    add("## 验收自检")
    add("")
    checks = payload["checks"]
    add(f"- 必需来源是否齐全：**{'是' if checks['all_required_present'] else '否'}**")
    for category, ids in checks.get("missing", {}).items():
        add(f"  - 缺失 {category}：{', '.join(ids)}")
    for marker in checks.get("markers", []):
        flag = "命中" if marker["found"] else "未命中"
        add(f"  - 标记「{marker['text']}」：{flag}（{marker['actual_source'] or '—'}）")
    add("")
    return "\n".join(lines)


def render_prompt(payload: dict[str, Any]) -> str:
    """生成给开发助手（MiniMax）的上下文文本，去掉给人看的排版噪音。"""
    lines: list[str] = []
    add = lines.append
    add(f"任务：{payload['task_id']} {payload['task_title']}")
    add("")
    add(payload["task_summary"])
    add("")
    add("必须遵守的规格条目：")
    for item in payload["sources"].get("spec", []):
        add(f"- {item['source_id']}（{item['path']} 第 {item['lines'][0]}-{item['lines'][1]} 行）")
        add(item["content"])
        add("")
    add("需要修改的代码：")
    for item in payload["sources"].get("code", []):
        add(f"- {item['source_id']}（第 {item['lines'][0]}-{item['lines'][1]} 行）")
        add("```python")
        add(item["content"].rstrip())
        add("```")
        add("")
    add("必须继续通过的既有测试：")
    for item in payload["sources"].get("test", []):
        if item["path"] in payload.get("regression_tests", []) or "existing" in item["path"]:
            add(f"- {item['source_id']}")
    add("")
    add("验收测试（不得修改）：")
    for path in payload.get("target_tests", []):
        add(f"- {path}")
    add("")
    add("当前任务状态：")
    add(f"- 已完成：{'；'.join(payload['state']['completed']) or '无'}")
    add(f"- 待办：{'；'.join(payload['state']['pending']) or '无'}")
    add("")
    return "\n".join(lines)
