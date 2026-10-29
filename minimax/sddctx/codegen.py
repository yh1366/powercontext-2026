"""用 MiniMax 生成代码修改，并把它落成可审计的补丁与快照。

产物（都在 ``runs/<run_id>/`` 与 ``patches/`` 下）：

* ``minimax.request.json``  —— 实际发出的 messages（含上下文包）
* ``minimax.response.json`` —— 原始响应、usage、request_id、耗时
* ``codegen.json``          —— 提取出的 diff、改动文件、校验结果
* ``patches/TASK-0007.patch`` —— 评审可直接 ``git apply`` 的补丁
* ``snapshots/before|after`` —— 修改前后文件快照 + sha256 清单

``--apply`` 会用本模块自带的 unified diff 解释器把补丁打到 ``demo_project``，
不依赖 git 是否安装；打完后同步 after 快照，所以「补丁、快照、工作区」三者一致。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sddctx.config import Settings
from sddctx.minimax_client import MiniMaxError, chat, system_prompt, user_prompt
from sddctx.textutil import now_iso, sha256_text

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(RuntimeError):
    """补丁无法应用。"""


@dataclass
class PatchReport:
    files: list[str]
    hunks: int
    added: int
    removed: int


def extract_diff(text: str) -> str:
    """从模型输出里取出 unified diff（容忍 ```diff 围栏与前后解释文字）。"""
    fenced = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    lines = candidate.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("--- ")), None)
    if start is None:
        raise PatchError("模型输出里没有找到 unified diff（缺少 '--- ' 行）")
    return "\n".join(lines[start:]).strip("\n") + "\n"


def _normalize_path(raw: str) -> str:
    path = raw.strip().split("\t")[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    if path.startswith("minimax/"):
        path = path[len("minimax/") :]
    if path.startswith("demo_project/"):
        path = path[len("demo_project/") :]
    return path


def _parse_hunks(lines: list[str]) -> tuple[str, list[tuple[int, list[str]]]]:
    old_path = _normalize_path(lines[0][4:])
    hunks: list[tuple[int, list[str]]] = []
    index = 1
    while index < len(lines):
        match = _HUNK_RE.match(lines[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("--- "):
            if lines[index].startswith("\\"):
                index += 1
                continue
            body.append(lines[index])
            index += 1
        hunks.append((old_start, body))
    return old_path, hunks


def _segment(body: list[str]) -> tuple[list[str], list[str]]:
    old_segment: list[str] = []
    new_segment: list[str] = []
    for line in body:
        if not line:
            continue
        tag, text = line[0], line[1:]
        if tag == " ":
            old_segment.append(text)
            new_segment.append(text)
        elif tag == "-":
            old_segment.append(text)
        elif tag == "+":
            new_segment.append(text)
        else:
            raise PatchError(f"无法识别的 diff 行：{line!r}")
    return old_segment, new_segment


def _locate(haystack: list[str], needle: list[str], expected: int) -> int | None:
    if not needle:
        return min(max(expected, 0), len(haystack))
    for candidate in (expected, *[expected + delta for delta in range(-40, 41)]):
        if 0 <= candidate <= len(haystack) - len(needle) and haystack[candidate : candidate + len(needle)] == needle:
            return candidate
    for candidate in range(0, len(haystack) - len(needle) + 1):
        if haystack[candidate : candidate + len(needle)] == needle:
            return candidate
    return None


def apply_unified_diff(root: Path, diff: str) -> PatchReport:
    """把 unified diff 打到 ``root`` 下（路径相对 root）。"""
    lines = [line for line in diff.splitlines()]
    files: list[str] = []
    hunks_total = 0
    added = 0
    removed = 0
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise PatchError("diff 头不完整：缺少 '+++ ' 行")
        old_path, hunks = _parse_hunks(lines[index:])
        if not hunks:
            index += 1
            continue
        target = root / old_path
        original_text = target.read_text(encoding="utf-8") if target.exists() else ""
        original_lines = original_text.splitlines()
        cursor = 0
        rebuilt: list[str] = []
        for old_start, body in hunks:
            old_segment, new_segment = _segment(body)
            position = _locate(original_lines, old_segment, old_start - 1)
            if position is None:
                raise PatchError(f"{old_path}: 第 {old_start} 行附近找不到上下文，补丁无法应用")
            if position < cursor:
                raise PatchError(f"{old_path}: hunk 顺序错乱（第 {old_start} 行）")
            rebuilt.extend(original_lines[cursor:position])
            rebuilt.extend(new_segment)
            cursor = position + len(old_segment)
            hunks_total += 1
            added += sum(1 for line in body if line.startswith("+"))
            removed += sum(1 for line in body if line.startswith("-"))
        rebuilt.extend(original_lines[cursor:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(rebuilt) + ("\n" if original_text.endswith("\n") or not original_text else ""), encoding="utf-8")
        files.append(old_path)
        index += 1
    if not files:
        raise PatchError("diff 里没有可应用的 hunk")
    return PatchReport(files=files, hunks=hunks_total, added=added, removed=removed)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(settings: Settings, relative_paths: list[str], dest: Path) -> dict[str, str]:
    if dest.exists():
        shutil.rmtree(dest)
    hashes: dict[str, str] = {}
    for rel in relative_paths:
        source = settings.root / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[rel] = _sha256(source)
    return hashes


def write_manifest(settings: Settings, task_id: str, before: dict[str, str], after: dict[str, str]) -> Path:
    manifest = {
        "task_id": task_id,
        "generated_at": now_iso(),
        "before": before,
        "after": after,
        "note": "before = 任务开始时的代码（目标测试失败）；after = 应用 MiniMax 补丁后的代码。",
    }
    path = settings.snapshots_dir / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def generate(
    settings: Settings,
    pack: Any,
    *,
    task_id: str,
    model: str | None = None,
    dry_run: bool = False,
    apply: bool = False,
    temperature: float = 0.1,
    extra_instruction: str = "",
) -> dict[str, Any]:
    payload = pack.to_dict()
    run_dir = settings.data_dir / payload["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_prompt(payload["prompt"], extra_instruction)},
    ]
    (run_dir / "minimax.request.json").write_text(
        json.dumps({"model": model or settings.minimax_model, "messages": messages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if dry_run:
        return {"run_id": payload["run_id"], "dry_run": True, "request_path": str(run_dir / "minimax.request.json")}

    try:
        result = chat(settings, messages, model=model, temperature=temperature)
    except MiniMaxError as exc:
        raise MiniMaxError(str(exc)) from exc

    (run_dir / "minimax.response.json").write_text(
        json.dumps(
            {
                "model": result.model,
                "latency_ms": result.latency_ms,
                "usage": result.usage,
                "request_id": result.request_id,
                "content": result.content,
                "raw": result.raw,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    diff = extract_diff(result.content)
    patch_path = settings.patches_dir / f"{task_id}.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {task_id} — 由 MiniMax（{result.model}）在 SDD 上下文下生成的代码修改\n"
        f"# 生成时间：{now_iso()}  |  request_id：{result.request_id or 'n/a'}\n"
        f"# 应用方式：cd minimax/demo_project && git apply -p1 ../patches/{task_id}.patch\n"
        f"# 也可以：python -m sddctx demo apply {task_id}\n"
    )
    patch_path.write_text(header + diff, encoding="utf-8")

    report: dict[str, Any] = {
        "run_id": payload["run_id"],
        "task_id": task_id,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "usage": result.usage,
        "request_id": result.request_id,
        "diff_sha256": sha256_text(diff),
        "patch_path": str(patch_path),
        "applied": False,
    }

    if apply:
        before_paths = sorted({item["path"] for category in ("code", "test") for item in payload["sources"][category]})
        pre_hashes = {
            rel: _sha256(settings.root / rel) for rel in before_paths if (settings.root / rel).is_file()
        }
        patched = apply_unified_diff(settings.demo_project, diff)
        changed = [f"demo_project/{path}" for path in patched.files]
        post_hashes = {rel: _sha256(settings.root / rel) for rel in changed}
        snapshot(settings, sorted(pre_hashes), settings.snapshots_dir / "before")
        snapshot(settings, changed, settings.snapshots_dir / "after")
        write_manifest(settings, task_id, pre_hashes, post_hashes)
        report.update(
            {
                "applied": True,
                "files": patched.files,
                "hunks": patched.hunks,
                "added_lines": patched.added,
                "removed_lines": patched.removed,
                "changed_hashes": post_hashes,
            }
        )

    (run_dir / "codegen.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _archive(settings, run_dir, ("minimax.request.json", "minimax.response.json", "codegen.json"))
    return report


def _archive(settings: Settings, run_dir: Path, names: tuple[str, ...]) -> Path:
    """把本次调用的关键证据复制到 docs/minimax-run/，随仓库提交。

    ``runs/`` 是运行产物目录（已 gitignore），但「MiniMax 实际参与」这条验收项
    需要评审在仓库里直接看到原始请求与响应，因此单独归档一份。
    """
    dest = settings.root / "docs" / "minimax-run" / run_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, dest / name)
    return dest
