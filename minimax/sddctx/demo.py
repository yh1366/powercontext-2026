"""验证样例的编排：把「失败 → 上下文 → 改代码 → 通过 → 状态复用」串成一条命令。

``sddctx demo sample`` 会按题面 2.1 的顺序走一遍，并且在每一步打印可核对的信息
（退出码、命中来源、状态事件数）。评审既可以直接跑这一条命令，也可以按
``docs/validation.md`` 里的分步命令逐条执行。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from sddctx.config import Settings
from sddctx.context import build_context, load_task
from sddctx.db import Database
from sddctx.ingest import ingest
from sddctx.state import StateStore, task_state_view
from sddctx.textutil import now_iso
from sddctx.verify import verify_task

MANIFEST = "manifest.json"


class SnapshotError(RuntimeError):
    pass


def _manifest(settings: Settings) -> dict[str, Any]:
    path = settings.snapshots_dir / MANIFEST
    if not path.is_file():
        raise SnapshotError(f"缺少快照清单：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _restore(settings: Settings, which: str) -> list[str]:
    manifest = _manifest(settings)
    hashes: dict[str, str] = manifest.get(which, {})
    if not hashes:
        raise SnapshotError(f"清单里没有 {which} 快照")
    restored: list[str] = []
    for rel in sorted(hashes):
        source = settings.snapshots_dir / which / rel
        if not source.is_file():
            raise SnapshotError(f"快照文件缺失：{source}")
        target = settings.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(rel)
    return restored


def reset_project(settings: Settings) -> list[str]:
    """把业务工程恢复到「修改前」：目标测试应当失败。"""
    return _restore(settings, "before")


def apply_snapshot(settings: Settings) -> list[str]:
    """把业务工程恢复到「修改后」：目标测试与回归测试都应当通过。"""
    return _restore(settings, "after")


def demo_state(settings: Settings, store: StateStore, task: dict[str, Any]) -> dict[str, Any]:
    return task_state_view(store, task)


def run_sample(
    settings: Settings,
    db: Database,
    store: StateStore,
    task_id: str,
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    task = load_task(settings, task_id)
    steps: list[dict[str, Any]] = []

    def note(step: str, detail: str) -> None:
        if verbose:
            print(f"  {step}: {detail}")

    if verbose:
        print("=" * 78)
        print(f"验证样例 · {task_id} {task.get('title', '')}")
        print("=" * 78)
        print("\n[0] 清空状态，从空状态开始记录")
    store.reset()
    steps.append({"step": "state_reset", "events": len(store.events(task_id))})
    note("state", f"事件数 {len(store.events(task_id))}")

    if verbose:
        print("\n[1] 恢复业务工程到修改前，运行测试（预期：目标用例失败、回归通过）")
    restored = reset_project(settings)
    steps.append({"step": "reset_before", "files": restored})
    note("reset", ", ".join(restored))
    before = verify_task(settings, db, store, task, phase="before")
    steps.append(
        {
            "step": "verify_before",
            "target_exit": before["target"]["exit_code"],
            "regression_exit": before["regression"]["exit_code"],
        }
    )
    note("目标测试", f"exit={before['target']['exit_code']}（预期非 0）")
    note("回归测试", f"exit={before['regression']['exit_code']}（预期 0）")

    if verbose:
        print("\n[2] 导入工程资料到 seekdb / OceanBase 并建立索引")
    report = ingest(settings, db, spec_variant=settings.spec_variant, reset=False)
    steps.append({"step": "ingest", **report.to_dict()})
    note("后端", f"{report.backend} / {db.label}")
    note(
        "资料",
        f"{report.documents} 个文档（本次重建 {len(report.changed)} 个 / 跳过 {report.unchanged} 个）、"
        f"索引块 {db.scalar('SELECT COUNT(*) FROM pc_chunks')} 个、检索词 {report.terms} 个",
    )

    if verbose:
        print("\n[3] 按任务挑选规格 / 代码 / 测试，组装开发上下文")
    pack = build_context(settings, db, store, task)
    steps.append({"step": "context", "run_id": pack.payload["run_id"], "checks": pack.payload["checks"]})
    for category in ("spec", "code", "test"):
        ids = [item["source_id"] for item in pack.payload["sources"][category]]
        note(f"{category} 来源", ", ".join(ids))
    note("验收自检", "通过" if pack.payload["checks"]["all_required_present"] else "不通过")
    store.append(task["task_id"], "step_done", step="S1")

    if verbose:
        print("\n[4] 应用 MiniMax 生成的代码修改（patches/%s.patch）" % task_id)
    applied = apply_snapshot(settings)
    store.append(task["task_id"], "patch_applied", files=applied, patch=f"patches/{task_id}.patch")
    store.append(task["task_id"], "step_done", step="S2")
    store.append(task["task_id"], "step_done", step="S3")
    note("改动文件", ", ".join(applied))

    if verbose:
        print("\n[5] 再次运行目标测试与回归测试（预期：全部通过）")
    after = verify_task(settings, db, store, task, phase="after")
    steps.append(
        {
            "step": "verify_after",
            "target_exit": after["target"]["exit_code"],
            "regression_exit": after["regression"]["exit_code"],
        }
    )
    note("目标测试", f"exit={after['target']['exit_code']}")
    note("回归测试", f"exit={after['regression']['exit_code']}")
    store.append(task["task_id"], "step_done", step="S4")

    if verbose:
        print("\n[6] 规格变更演练：导入 SPEC-003 v1.1 后重新组装上下文")
    ingest(settings, db, spec_variant="v2", reset=False)
    pack_v2 = build_context(settings, db, store, task)
    steps.append({"step": "context_v2", "checks": pack_v2.payload["checks"]})
    note("规格版本", pack_v2.payload["spec_variant"])
    note("标记校验", json.dumps(pack_v2.payload["checks"]["markers"], ensure_ascii=False))
    ingest(settings, db, spec_variant=settings.spec_variant, reset=False)

    if verbose:
        print("\n[7] 状态复用：从状态后端重新读取任务进展")
    view = demo_state(settings, store, task)
    steps.append({"step": "state_reuse", "completed": view["completed"], "pending": view["pending"]})
    note("状态后端", f"{view['backend']} / {view['scope']}")
    note("已完成", "; ".join(view["completed"]) or "无")
    note("待办", "; ".join(view["pending"]) or "无")
    note("验证记录", json.dumps(view["verifications"], ensure_ascii=False))

    result = {
        "task_id": task_id,
        "generated_at": now_iso(),
        "storage": db.backend,
        "state_backend": store.backend,
        "spec_variant": pack.payload["spec_variant"],
        "before_all_passed": before["all_passed"],
        "after_all_passed": after["all_passed"],
        "steps": steps,
        "ok": (not before["all_passed"]) and after["all_passed"] and pack.payload["checks"]["all_required_present"],
    }
    out = settings.data_dir / "sample-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print("\n" + "=" * 78)
        print(
            "结论：修改前目标测试失败 = "
            f"{not before['all_passed']}；修改后全部通过 = {after['all_passed']}；"
            f"上下文自检 = {pack.payload['checks']['all_required_present']}"
        )
        print(f"报告：{out}")
        print("=" * 78)
    return result
