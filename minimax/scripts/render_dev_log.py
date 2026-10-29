"""从 runs/ 下的落盘记录生成 docs/minimax-dev-log.md（MiniMax 开发记录）。

   cd minimax && python scripts/render_dev_log.py

数据来源（全部由 ``python -m sddctx codegen`` 自动落盘，不手工粘贴）：

* ``runs/<run_id>/minimax.request.json``  —— 实际发送的 model 与 messages
* ``runs/<run_id>/minimax.response.json`` —— 原始响应、usage、request_id、耗时
* ``runs/<run_id>/codegen.json``          —— 提取出的 diff 摘要、改动文件、行数
* ``runs/<run_id>/context.json``          —— 该次使用的上下文包（哪些来源被喂给模型）
* ``patches/TASK-0007.patch``             —— 最终补丁

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


def main() -> int:
    settings = load_settings()
    patches = sorted(settings.patches_dir.glob("TASK-*.patch"))
    runs: list[dict] = []
    for codegen_path in sorted(settings.data_dir.glob("*/codegen.json")):
        run_dir = codegen_path.parent
        request = _load(run_dir / "minimax.request.json")
        response = _load(run_dir / "minimax.response.json")
        codegen = _load(codegen_path)
        context = _load(run_dir / "context.json")
        if not response:
            continue
        runs.append(
            {
                "run_dir": run_dir.name,
                "request": request,
                "response": response,
                "codegen": codegen,
                "context": context,
            }
        )

    lines: list[str] = []
    add = lines.append
    add("# MiniMax 开发记录")
    add("")
    add("本记录由 `scripts/render_dev_log.py` 从 `runs/` 下的落盘文件自动生成，")
    add("内容与提交的 `patches/` 补丁、`docs/logs/` 测试日志一一对应。")
    add("")

    add("## 1. 接入方式")
    add("")
    add("| 项目 | 值 |")
    add("| --- | --- |")
    add("| 接口 | `POST {base}/v1/text/chatcompletion_v2`（MiniMax 开放平台 chatcompletion_v2 协议） |")
    add("| 模型 | `" + (runs[0]["response"].get("model") if runs else settings.minimax_model) + "` |")
    add("| 客户端 | `sddctx/minimax_client.py`，标准库 urllib，无 SDK |")
    add("| 温度 / 上限 | temperature=0.1，max_tokens=8000 |")
    add("| 产物 | `runs/<run_id>/minimax.request.json`、`minimax.response.json`、`codegen.json` |")
    add("")

    if not runs:
        add("## 2. 生成记录")
        add("")
        add("> **待补充**：当前仓库尚未包含一次真实的生成记录（`MINIMAX_API_KEY` 申请中）。")
        add("> Key 就绪后执行下面的命令即可自动补齐本节：")
        add("")
        add(FENCE + "bash")
        add("cd minimax")
        add("python -m sddctx demo reset                      # 确保工作区处于修改前状态")
        add("python -m sddctx codegen --task TASK-0007 --apply # 调 MiniMax 出补丁并落地快照")
        add("python scripts/render_dev_log.py")
        add(FENCE)
        add("")
        add("在此之前，`patches/TASK-0007.reference.patch` 是人工撰写的参考实现，")
        add("仅用于打通「上下文 → 改代码 → 验证」链路，**不作为 MiniMax 参与的证据**。")
    else:
        latest = runs[-1]
        context = latest["context"]
        codegen = latest["codegen"]
        response = latest["response"]
        add("## 2. 任务与上下文")
        add("")
        add(f"- 任务：`{context.get('task_id', 'TASK-0007')}` {context.get('task_title', '')}")
        add(f"- 上下文编号：`{context.get('run_id', latest['run_dir'])}`（规格版本 `{context.get('spec_variant', '')}`）")
        add(f"- 资料库：`{context.get('storage', {}).get('backend', '')}` / 库 `{context.get('storage', {}).get('database', '')}`")
        add("")
        add("喂给模型的三类来源（与 `docs/validation.md` 的核对清单一致）：")
        add("")
        add("| 类别 | 来源 ID | 位置 |")
        add("| --- | --- | --- |")
        for category in ("spec", "code", "test"):
            for item in context.get("sources", {}).get(category, []):
                lines_range = item.get("lines") or ["", ""]
                add(f"| {category} | `{item['source_id']}` | `{item['path']}` L{lines_range[0]}–L{lines_range[1]} |")
        add("")
        add(f"检索 trace：`{json.dumps(context.get('query', {}).get('traces', {}).get('spec', {}).get('steps', []))}`（spec 类）")
        add("")

        add("## 3. 提示词（摘要）")
        add("")
        messages = latest["request"].get("messages", [])
        add(f"- messages 条数：{len(messages)}（system + user）")
        add(f"- user 消息字符数：{len(messages[-1]['content']) if messages else 0}")
        add("")
        add("system 提示词：")
        add("")
        add(FENCE)
        add(messages[0]["content"].strip() if messages else "")
        add(FENCE)
        add("")
        add("user 消息（前 40 行，完整内容见 `runs/" + latest["run_dir"] + "/minimax.request.json`）：")
        add("")
        add(FENCE)
        add("\n".join((messages[-1]["content"] if messages else "").splitlines()[:40]))
        add("…")
        add(FENCE)
        add("")

        add("## 4. 模型响应")
        add("")
        add("| 指标 | 值 |")
        add("| --- | --- |")
        add(f"| model | `{response.get('model', '')}` |")
        add(f"| request_id | `{response.get('request_id', '') or 'n/a'}` |")
        add(f"| 端到端耗时 | {response.get('latency_ms', 0)} ms |")
        add(f"| usage | `{json.dumps(response.get('usage', {}), ensure_ascii=False)}` |")
        add(f"| 输出字符数 | {len(response.get('content', ''))} |")
        add("")
        add("模型输出原文（完整见 `runs/" + latest["run_dir"] + "/minimax.response.json`）：")
        add("")
        add(FENCE + "diff")
        add(response.get("content", "").strip())
        add(FENCE)
        add("")

        add("## 5. 提取出的补丁")
        add("")
        add(f"- 补丁文件：`{codegen.get('patch_path', '')}`")
        add(f"- diff sha256：`{codegen.get('diff_sha256', '')}`")
        if codegen.get("applied"):
            add(f"- 应用结果：{codegen.get('hunks')} 个 hunk，+{codegen.get('added_lines')} / -{codegen.get('removed_lines')} 行，"
                f"改动文件 {', '.join(codegen.get('files', []))}")
        add("")
        for patch in patches:
            text = patch.read_text(encoding="utf-8")
            add(f"### `patches/{patch.name}`")
            add("")
            add(FENCE + "diff")
            add("\n".join(text.splitlines()[:80]))
            add(FENCE)
            add("")

        add("## 6. 人工复核与后续修正")
        add("")
        add("- 生成后先 `python -m sddctx demo apply` 落地快照，再跑目标测试与回归测试；")
        add("- 若模型输出未覆盖某条规格（例如累计上限的告警文本），会在下一轮把该条款重新强调后重跑，")
        add("  每一轮的 request/response 都留在 `runs/` 下，可追溯；")
        add("- 最终提交的 `patches/TASK-0007.patch` 与 `snapshots/` 必须与最后一轮响应一致。")

    add("")
    add("## 附：如何复跑")
    add("")
    add(FENCE + "bash")
    add("cd minimax")
    add('export MINIMAX_API_KEY=...           # 或用 .env.local')
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
