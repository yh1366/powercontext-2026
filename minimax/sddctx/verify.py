"""测试执行：把「命令 + 退出码 + 结论」变成可审计的验证结果。

评审的口径是「同一组目标功能测试在修改前失败、修改后通过」，所以每次执行都记录
命令原文、退出码、用例统计与代码版本（git commit），并写进 PowerContext 状态，
后续会话可以直接读出「上次验证用的是哪个版本、结果如何」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sddctx.config import Settings
from sddctx.db import Database
from sddctx.state import StateStore
from sddctx.textutil import now_iso

_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def _git_commit(root: Path) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        output = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return output.stdout.strip()
    except Exception:
        return ""


def _parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value, label in _COUNT_RE.findall(output):
        key = "error" if label.startswith("error") else label
        counts[key] = int(value)
    return counts


def run_pytest(settings: Settings, targets: list[str], *, timeout: int = 300) -> dict[str, Any]:
    if not targets:
        return {
            "command": "",
            "exit_code": None,
            "passed": None,
            "output": "(no target given)",
            "duration_ms": 0,
            "counts": {},
        }
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
    # 关掉第三方 pytest 插件自动加载：验证结果不应随评审机器上装了什么插件而变化，
    # 也不希望 asyncio/随机序之类的插件的告警混进判决日志。
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=settings.root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    duration = int((time.time() - started) * 1000)
    output = "\n".join(
        line
        for line in ((completed.stdout or "") + (completed.stderr or "")).splitlines()
        if "PytestDeprecationWarning" not in line and "warnings.warn(" not in line
    )
    counts = _parse_counts(output)
    return {
        "command": " ".join(["python", "-m", "pytest", "-q", *targets]),
        "argv": command,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "counts": counts,
        "duration_ms": duration,
        "output_tail": output.strip().splitlines()[-25:],
        "output": output,
    }


def verify_task(
    settings: Settings,
    db: Database,
    store: StateStore,
    task: dict[str, Any],
    *,
    phase: str = "manual",
    run_id: str = "",
    record: bool = True,
) -> dict[str, Any]:
    target = run_pytest(settings, list(task.get("target_tests", [])))
    regression = run_pytest(settings, list(task.get("regression_tests", [])))
    all_passed = bool(target["passed"]) and bool(regression["passed"])
    summary = (
        f"目标测试 {'通过' if target['passed'] else '失败'}"
        f"（exit={target['exit_code']}），回归测试 {'通过' if regression['passed'] else '失败'}"
        f"（exit={regression['exit_code']}）"
    )
    result = {
        "task_id": task["task_id"],
        "phase": phase,
        "verified_at": now_iso(),
        "commit": _git_commit(settings.root),
        "python": sys.version.split()[0],
        "target": {key: value for key, value in target.items() if key != "output"},
        "regression": {key: value for key, value in regression.items() if key != "output"},
        "all_passed": all_passed,
        "summary": summary,
    }

    run_id = run_id or f"verify-{now_iso().replace(':', '').replace('-', '')[0:15]}-{task['task_id']}"
    result["run_id"] = run_id
    run_dir = settings.data_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"verify.{phase}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / f"pytest.{phase}.log").write_text(
        "$ " + target["command"] + "\n" + target.get("output", "") + "\n\n"
        "$ " + regression["command"] + "\n" + regression.get("output", ""),
        encoding="utf-8",
    )

    if record:
        store.append(
            task["task_id"],
            "verify",
            phase=phase,
            exit_code=target["exit_code"],
            passed=all_passed,
            command=f"{target['command']}  &&  {regression['command']}",
            summary=summary,
            commit=result["commit"],
            run_id=run_id,
        )
        db.put_state(
            f"verify:{task['task_id']}:{phase}",
            json.dumps(
                {key: result[key] for key in ("phase", "all_passed", "verified_at", "commit", "summary", "run_id")},
                ensure_ascii=False,
            ),
        )
    return result
