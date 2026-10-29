"""任务状态：写进 PowerContext，重启后读回并参与下一轮上下文。

状态不是「日志文件」而是可以驱动下一步动作的记录：

* ``task_started``、``step_done``     → 哪些步骤已完成、还剩什么
* ``verify``                          → 每次测试的命令、退出码、结论
* ``context_built``                   → 上次用的是哪一版规格、哪些来源
* ``patch_applied``                   → 代码修改是否已经落地

存储后端有两条：``powercontext``（题目要求的主路径）与 ``file``（离线降级，便于评审
在没有 Server 的环境里也能验证「状态复用」这条链路本身）。降级原因会写进输出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sddctx.config import Settings
from sddctx.pc_client import STATE_PREFIX, PowerContextClient, PowerContextError

KIND = "sdd-task-state"


class StateError(RuntimeError):
    """状态后端不可用。"""


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


class StateStore(Protocol):
    backend: str
    scope_label: str

    def append(self, task_id: str, event: str, **payload: Any) -> dict[str, Any]: ...

    def events(self, task_id: str | None = None) -> list[dict[str, Any]]: ...

    def reset(self) -> None: ...


@dataclass
class FileStore:
    """把事件追加到 ``runs/state/events.jsonl``，用于无 Server 的降级验证。"""

    path: Path

    @property
    def backend(self) -> str:
        return "file"

    @property
    def scope_label(self) -> str:
        return str(self.path)

    def append(self, task_id: str, event: str, **payload: Any) -> dict[str, Any]:
        record = {"task_id": task_id, "event": event, "payload": payload, "at": _stamp()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def events(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return _after_reset(records, task_id)

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")


class PowerContextStore:
    """把每条状态写成一条 PowerContext 记忆，读回时按前缀还原。"""

    def __init__(self, client: PowerContextClient, scope_id: str) -> None:
        self.client = client
        self.scope_id = scope_id

    @property
    def backend(self) -> str:
        return "powercontext"

    @property
    def scope_label(self) -> str:
        return self.scope_id

    def append(self, task_id: str, event: str, **payload: Any) -> dict[str, Any]:
        record = {"task_id": task_id, "event": event, "payload": payload, "at": _stamp()}
        text = f"{STATE_PREFIX} {json.dumps(record, ensure_ascii=False)}"
        self.client.remember(self.scope_id, KIND, text)
        return record

    def events(self, task_id: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for entry in self.client.list_entries(self.scope_id):
            text = (entry.get("text") or "").strip()
            if not text.startswith(STATE_PREFIX):
                continue
            try:
                records.append(json.loads(text[len(STATE_PREFIX) :].strip()))
            except json.JSONDecodeError:
                continue
        records.sort(key=lambda item: item.get("at", ""))
        return _after_reset(records, task_id)

    def reset(self) -> None:
        self.append("__global__", "state_reset")


def _after_reset(records: list[dict[str, Any]], task_id: str | None) -> list[dict[str, Any]]:
    last_reset = -1
    for index, record in enumerate(records):
        if record.get("event") == "state_reset":
            last_reset = index
    selected = records[last_reset + 1 :]
    if task_id:
        selected = [record for record in selected if record.get("task_id") == task_id]
    return selected


def open_state(settings: Settings) -> tuple[StateStore, str]:
    """返回 (store, fallback_reason)。``auto`` 下 PowerContext 不可用会降级到文件。"""
    choice = (settings.state_backend or "auto").lower()
    reason = ""
    if choice in ("powercontext", "auto"):
        client = PowerContextClient(settings.powercontext_url)
        if client.health():
            try:
                scope_id = settings.powercontext_scope_id or client.ensure_scope(
                    settings.powercontext_scope_key,
                    "sddctx / ordercore 任务状态",
                    "SDD 上下文模块的任务进展与验证结果，供跨会话恢复使用。",
                )
                return PowerContextStore(client, scope_id), ""
            except PowerContextError as exc:
                reason = f"PowerContext scope 初始化失败：{exc}"
        else:
            reason = f"PowerContext 不可达：{settings.powercontext_url}"
        if choice == "powercontext":
            raise StateError(reason)
    data_dir = settings.data_dir
    return FileStore(data_dir / "state" / "events.jsonl"), reason


def task_state_view(store: StateStore, task: dict[str, Any], fallback_reason: str = "") -> dict[str, Any]:
    """把事件流折叠成「已完成 / 待办 / 验证结果」，供上下文与 status 复用。"""
    events = store.events(task.get("task_id"))
    step_labels = {step["id"]: step["label"] for step in task.get("steps", [])}
    done_steps = {record["payload"].get("step") for record in events if record.get("event") == "step_done"}
    verifications = [
        {
            "phase": record["payload"].get("phase", ""),
            "exit_code": record["payload"].get("exit_code"),
            "passed": record["payload"].get("passed"),
            "command": record["payload"].get("command", ""),
            "summary": record["payload"].get("summary", ""),
            "at": record.get("at", ""),
        }
        for record in events
        if record.get("event") == "verify"
    ]
    contexts = [
        {
            "run_id": record["payload"].get("run_id"),
            "spec_variant": record["payload"].get("spec_variant"),
            "sources": record["payload"].get("sources"),
            "at": record.get("at", ""),
        }
        for record in events
        if record.get("event") == "context_built"
    ]
    return {
        "backend": store.backend,
        "scope": store.scope_label,
        "fallback_reason": fallback_reason,
        "event_count": len(events),
        "completed": [label for step_id, label in step_labels.items() if step_id in done_steps],
        "pending": [label for step_id, label in step_labels.items() if step_id not in done_steps],
        "verifications": verifications,
        "last_context": contexts[-1] if contexts else None,
        "last_events": events[-4:],
    }
