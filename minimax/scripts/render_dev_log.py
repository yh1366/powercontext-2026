"""从 runs/ 下的落盘记录生成 docs/minimax-dev-log.md（MiniMax 开发记录）。

   cd minimax && python scripts/render_dev_log.py

数据来源（全部由 ``python -m sddctx codegen`` 自动落盘，不手工粘贴）：

* ``runs/<run_id>/minimax.request*.json``  —— 实际发送的 model 与 messages（含上下文包）
* ``runs/<run_id>/minimax.response*.json`` —— 原始响应、usage、request_id、耗时
* ``runs/<run_id>/codegen.json``           —— 提取出的 diff 摘要、改动文件、行数、每轮结果
* ``runs/<run_id>/context.json``           —— 该次使用的上下文包（哪些来源被喂给模型）
* ``patches/TASK-0007.patch``              —— 最终补丁

生成结果是「上下文 → 提示词摘要 → 响应摘要 → diff」一一对应的记录，
评审可以拿 docs/logs 里的测试日志与 patches/ 里的补丁交叉核对。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sddctx.config import load_settings  # noqa: E402

FENCE = "```"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _attempt_pairs(run_dir: Path) -> list[tuple[int, dict, dict]]:
    """收集该 run 下所有 (轮次, 请求, 响应)，按轮次排序。"""
    pairs: list[tuple[int, dict, dict]] = []
    for request_path in sorted(run_dir.glob("minimax.request*.json")):
        suffix = request_path.stem.replace("minimax.request", "")
        attempt = int(suffix.replace(".attempt", "")) if suffix else 1
        response_path = run_dir / request_path.name.replace("minimax.request", "minimax.response")
        response = _load(response_path)
        if not response:
            continue
        pairs.append((attempt, _load(request_path), response))
    return sorted(pairs)


def collect(settings) -> list[dict]:
    runs: list[dict] = []
    for run_dir in sorted(settings.data_dir.glob("run-*")):
        pairs = _attempt_pairs(run_dir)
        if not pairs:
            continue
        runs.append(
            {
                "run_dir": run_dir.name,
                "attempts": pairs,
                "codegen": _load(run_dir / "codegen.json"),
                "context": _load(run_dir / "context.json"),
            }
        )
    return runs


def main() -> int:
    settings = load_settings()
    patches = sorted(settings.patches_dir.glob("TASK-*.patch"))
    runs = collect(settings)
    final = runs[-1] if runs else None
    final_attempt = final["attempts"][-1][2] if final else {}

    lines: list[str] = []
    add = lines.append
    add("# MiniMax 开发记录")
    add("")
    add("本记录由 `scripts/render_dev_log.py` 从 `runs/` 下的落盘文件自动生成，")
    add("内容与提交的 `patches/` 补丁、`docs/logs/` 测试日志一一对应，未做人工改写。")
    add("")
    add("## 1. 接入方式")
    add("")
    add("| 项目 | 值 |")
    add("| --- | --- |")
    add("| 接口 | `POST {MINIMAX_BASE_URL}/v1/chat/completions`（OpenAI 兼容；服务端不支持时回退 `chatcompletion_v2`） |")
    add("| Base URL | `" + settings.minimax_base_url + "` |")
    add(
        f"| 模型 | `{final_attempt.get('model', settings.minimax_model)}`"
        "（`python -m sddctx models` 可列出该 Key 下全部可用模型） |"
    )
    add("| 客户端 | `sddctx/minimax_client.py`，标准库 urllib，无 SDK 依赖 |")
    add("| 参数 | temperature=0.1，max_tokens=8000，stream=false |")
    add("| 产物 | `runs/<run_id>/minimax.request*.json`、`minimax.response*.json`、`codegen.json`，并归档到 `docs/minimax-run/` |")
    add("")

    if not runs:
        add("## 2. 生成记录")
        add("")
        add("> 当前仓库没有生成记录。执行下面的命令即可自动补齐：")
        add("")
        add(FENCE + "bash")
        add("cd minimax")
        add("python -m sddctx demo reset")
        add("python -m sddctx codegen --task TASK-0007 --apply")
        add("python scripts/render_dev_log.py")
        add(FENCE)
        add("")
        out = ROOT / "docs" / "minimax-dev-log.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"written {out} (0 run)")
        return 0

    add("## 2. 生成轮次一览")
    add("")
    add("每次调用都单独落盘（一次成功就是 1 行，需要重试时每轮各占 1 行），"
        "便于核对「模型看到了什么、错在哪、怎么修」。")
    add("")
    add("| 轮 | run_id | request_id | 耗时 | prompt / completion | 结果 |")
    add("| --- | --- | --- | --- | --- | --- |")
    codegen_of = {run["run_dir"]: run["codegen"] for run in runs}
    for run in runs:
        attempts_report = {item["attempt"]: item for item in codegen_of[run["run_dir"]].get("attempts", [])}
        for attempt, _request, response in run["attempts"]:
            info = attempts_report.get(attempt, {})
            usage = response.get("usage", {})
            if info.get("applied"):
                verdict = "补丁已应用"
            elif info.get("error"):
                verdict = "补丁无法应用：`" + str(info["error"])[:60].replace("|", "/") + "`"
            else:
                verdict = "仅落盘请求（未调用）" if not codegen_of[run["run_dir"]] else "未应用"
            add(
                f"| {attempt} | `{run['run_dir']}` | `{(response.get('request_id') or 'n/a')[:16]}` | "
                f"{response.get('latency_ms', 0)} ms | {usage.get('prompt_tokens', 0)} / {usage.get('completion_tokens', 0)} | {verdict} |"
            )
    add("")
    add("> 说明：最初上下文只给了「按符号切出来的代码片段」，模型按片段猜行号，输出的 hunk")
    add("> 在 `@@ -8` 附近找不到上下文行、补丁打不上（`PatchError`）。把「目标文件完整内容（带行号）」")
    add("> 加进上下文后，同一任务一次应用成功——这也是 `context.files` 字段的由来。")
    add("")

    add("## 3. 上下文（喂给模型的第一轮输入）")
    add("")
    context = final["context"]
    add(f"- 任务：`{context.get('task_id', 'TASK-0007')}` {context.get('task_title', '')}")
    add(f"- 上下文编号：`{context.get('run_id', final['run_dir'])}`（规格版本 `{context.get('spec_variant', '')}`）")
    add(
        f"- 资料库：`{context.get('storage', {}).get('backend', '')}` / 库 "
        f"`{context.get('storage', {}).get('database', '')}`，检索结果在 `query.traces` 里带命中行数"
    )
    add("")
    add("| 类别 | 来源 ID | 位置 |")
    add("| --- | --- | --- |")
    for category in ("spec", "code", "test"):
        for item in context.get("sources", {}).get(category, []):
            line_range = item.get("lines") or ["", ""]
            add(f"| {category} | `{item['source_id']}` | `{item['path']}` L{line_range[0]}–L{line_range[1]} |")
    for item in context.get("files", []):
        add(f"| 完整文件 | `{item['path']}` | L1–L{item['lines'][1]}（`{item['sha256'][:12]}`） |")
    add("")
    add(
        "检索 trace（spec 类）：`"
        + json.dumps(context.get("query", {}).get("traces", {}).get("spec", {}).get("steps", []))
        + "`"
    )
    add("")

    final_run = final
    final_attempt_no, final_request, final_response = final_run["attempts"][-1]
    messages = final_request.get("messages", [])

    add(f"## 4. 提示词（最终轮 = 第 {final_attempt_no} 轮）")
    add("")
    add(f"- messages 条数：{len(messages)}（system + user）")
    add(f"- user 消息字符数：{len(messages[-1]['content']) if messages else 0}")
    add("")
    add("system 提示词：")
    add("")
    add(FENCE)
    add(messages[0]["content"].strip() if messages else "")
    add(FENCE)
    add("")
    add(
        "user 消息（前 40 行，完整内容见 `runs/"
        + final_run["run_dir"]
        + f"/minimax.request{'' if final_attempt_no == 1 else '.attempt%d' % final_attempt_no}.json`）："
    )
    add("")
    add(FENCE)
    add("\n".join((messages[-1]["content"] if messages else "").splitlines()[:40]))
    add("…")
    add(FENCE)
    add("")

    add("## 5. 模型响应（最终轮）")
    add("")
    add("| 指标 | 值 |")
    add("| --- | --- |")
    add(f"| model | `{final_response.get('model', '')}` |")
    add(f"| endpoint | `{final_response.get('endpoint', '')}` |")
    add(f"| request_id | `{final_response.get('request_id', '') or 'n/a'}` |")
    add(f"| 端到端耗时 | {final_response.get('latency_ms', 0)} ms |")
    add(f"| usage | `{json.dumps(final_response.get('usage', {}), ensure_ascii=False)}` |")
    add(f"| 输出字符数 | {len(final_response.get('content', ''))} |")
    add("")
    add("模型输出原文：")
    add("")
    add(FENCE + "diff")
    add(final_response.get("content", "").strip())
    add(FENCE)
    add("")

    add("## 6. 提取出的补丁")
    add("")
    codegen = final_run["codegen"]
    add(f"- 补丁文件：`{codegen.get('patch_path', '')}`")
    add(f"- diff sha256：`{codegen.get('diff_sha256', '')}`")
    if codegen.get("applied"):
        add(
            f"- 应用结果：{codegen.get('hunks')} 个 hunk，+{codegen.get('added_lines')} / -{codegen.get('removed_lines')} 行，"
            f"改动文件 {', '.join(codegen.get('files', []))}"
        )
    add("")
    for patch in patches:
        text = patch.read_text(encoding="utf-8")
        add(f"### `patches/{patch.name}`")
        add("")
        add(FENCE + "diff")
        add("\n".join(text.splitlines()[:80]))
        add(FENCE)
        add("")

    add("## 7. 人工复核与后续验证")
    add("")
    add("- 每轮响应都先经过 `apply_unified_diff` 校验能否真正落到代码上，再进测试；")
    add("- 生成后依次执行 `demo apply` → 目标测试 → 回归测试 → `verify --phase after`，结果写回 PowerContext；")
    add("- 提交的 `patches/TASK-0007.patch`、`snapshots/` 与最后一轮响应保持一致。")
    add("")
    add("## 附：如何复跑")
    add("")
    add(FENCE + "bash")
    add("cd minimax")
    add("export MINIMAX_API_KEY=...           # 或用 .env.local（不入库）")
    add("python -m sddctx demo reset           # 回到修改前")
    add("python -m sddctx codegen --task TASK-0007 --apply")
    add("python scripts/render_dev_log.py")
    add(FENCE)
    add("")

    out = ROOT / "docs" / "minimax-dev-log.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"written {out} ({len(runs)} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
