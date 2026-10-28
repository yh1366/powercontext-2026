# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pi session branch history as immutable, individually citable Sources.

A Pi v3 session archive is a parent-linked graph. One selected leaf and its
ancestor chain form the transcript a user actually saw, so this module turns
every visible event on that chain into its own ``CAPTURED`` Source identified
by the session and the event. Control nodes stay on the graph but produce no
text evidence, and the connector keeps only committed progress in the
ConnectorLifecycle checkpoint so a restart never re-imports or overwrites an
already persisted event.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_TYPE = "pi-event"
PI_EVENT_DEFINITION_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_VERSION = 3
SOURCE_ID_PREFIX = "pi"
SOURCE_ID_SEPARATOR = ":"

CHECKPOINT_VERSION = 1
_TEXT_BLOCK = "text"
_TOOL_CALL_BLOCK = "toolCall"
_UNKNOWN_TOOL = "unknown"


class PiSessionInput(BaseModel):
    """One complete offline Pi v3 archive plus the branch leaf to follow."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its original record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A single captured session event with a stable ``pi:session:event`` identity."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    definition_version: str = PI_EVENT_DEFINITION_VERSION
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity ``pi:<session>:<event>`` for one visible event."""

    _require_identity_part("session_id", session_id)
    _require_identity_part("event_id", event_id)
    identity = (
        f"{SOURCE_ID_PREFIX}{SOURCE_ID_SEPARATOR}{quote(session_id, safe='')}"
        f"{SOURCE_ID_SEPARATOR}{quote(event_id, safe='')}"
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi event source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pi_session(value: PiSessionInput | bytes) -> tuple[PiEventInput, ...]:
    """Parse one archive atomically and return the chosen branch's visible events."""

    if not isinstance(value, PiSessionInput):
        value = PiSessionInput(content=value)
    session_id, records = _load_archive(value.content)
    return _branch_events(session_id, records, value.leaf_id)


def _load_archive(content: bytes) -> tuple[str, list[dict[str, JsonValue]]]:
    text = _decode_archive(content)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("pi session archive must not be empty")  # noqa: TRY003
    documents = [_parse_line(line) for line in lines]

    header = documents[0]
    if header.get("type") != "session":
        raise ValueError("pi session archive must start with a session header")  # noqa: TRY003
    if header.get("version") != PI_SESSION_VERSION:
        raise ValueError(f"pi session archive must declare version {PI_SESSION_VERSION}")  # noqa: TRY003
    session_id = header.get("id")
    _require_identity_part("session id", session_id)

    records: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    for document in documents[1:]:
        event_id = document.get("id")
        _require_identity_part("event id", event_id)
        if event_id in seen:
            raise ValueError(f"pi session archive repeats event id {event_id!r}")  # noqa: TRY003
        parent_id = document.get("parentId")
        if parent_id is not None:
            _require_identity_part("parentId", parent_id)
            if parent_id not in seen:
                raise ValueError(f"pi event {event_id!r} references unknown parent {parent_id!r}")  # noqa: TRY003
        seen.add(event_id)
        records.append(document)
    return session_id, records


def _branch_events(
    session_id: str,
    records: list[dict[str, JsonValue]],
    leaf_id: str | None,
) -> tuple[PiEventInput, ...]:
    chain = _branch_chain(records, leaf_id)
    events: list[PiEventInput] = []
    for record in chain:
        visible = _visible_text(record)
        if visible is None:
            continue
        role, body = visible
        event_id = str(record["id"])
        parent = record.get("parentId")
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=None if parent is None else str(parent),
                event_type=str(record.get("type")),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


def _branch_chain(
    records: list[dict[str, JsonValue]],
    leaf_id: str | None,
) -> list[dict[str, JsonValue]]:
    if not records:
        raise ValueError("pi session archive has no event to select as leaf")  # noqa: TRY003
    by_id = {str(record["id"]): record for record in records}
    if leaf_id is None:
        leaf_id = str(records[-1]["id"])
    elif leaf_id not in by_id:
        raise ValueError(f"pi session archive has no event named {leaf_id!r}")  # noqa: TRY003

    chain: list[dict[str, JsonValue]] = []
    visited: set[str] = set()
    cursor: str | None = leaf_id
    while cursor is not None:
        if cursor in visited:
            raise ValueError(f"pi session archive has a parent cycle at {cursor!r}")  # noqa: TRY003
        visited.add(cursor)
        record = by_id.get(cursor)
        if record is None:
            raise ValueError(f"pi session archive is missing parent {cursor!r}")  # noqa: TRY003
        chain.append(record)
        parent = record.get("parentId")
        cursor = None if parent is None else str(parent)
    chain.reverse()
    return chain


def _visible_text(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = record.get("type")
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        return _visible_pair(str(event_type), summary)

    if event_type == "custom_message":
        return _visible_pair("custom", _block_text(record.get("content")))

    if event_type != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role.strip():
        return None
    if role == "bashExecution":
        return _visible_pair(role, _bash_text(message))
    if role == "toolResult":
        return _visible_pair(role, _tool_result_text(message))
    return _visible_pair(role, _block_text(message.get("content")))


def _visible_pair(role: str, body: object) -> tuple[str, str] | None:
    if not isinstance(body, str) or not body.strip():
        return None
    return role, body


def _block_text(content: object) -> str:
    """Render visible message content blocks, keeping their order."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == _TEXT_BLOCK:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == _TOOL_CALL_BLOCK:
            parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, JsonValue]) -> str:
    name = block.get("name")
    call_id = block.get("id")
    arguments = block.get("arguments")
    return (
        f"[tool_call {_text_or_unknown(name)} {_text_or_unknown(call_id)}]"
        f" {compact_json(arguments if arguments is not None else {})}"
    )


def _tool_result_text(message: dict[str, JsonValue]) -> str:
    head = f"[tool_result {_text_or_unknown(message.get('toolName'))} {_text_or_unknown(message.get('toolCallId'))}]"
    body = _block_text(message.get("content"))
    return f"{head} {body}" if body else head


def _bash_text(message: dict[str, JsonValue]) -> str:
    command = message.get("command")
    output = message.get("output")
    head = f"$ {command if isinstance(command, str) else ''}"
    if isinstance(output, str) and output:
        return f"{head}\n{output}"
    return head


def _text_or_unknown(value: object) -> str:
    return value if isinstance(value, str) and value else _UNKNOWN_TOOL


def compact_json(value: object) -> str:
    """Serialize tool arguments with sorted keys, Unicode kept, no extra spaces."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("pi tool arguments must be JSON serializable") from error  # noqa: TRY003


class PiEventSourceAdapter:
    """Resolve and read the session event Source described by one Definition."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        _require_event(value)
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one session event as canonical standard text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_TYPE,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import the visible events of one selected Pi branch incrementally."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, records = _load_archive(self.path.read_bytes())
        events = _branch_events(session_id, records, self.leaf_id)
        imported, stored_session = _imported_events(session.checkpoint)
        if stored_session is not None and stored_session != session_id:
            imported = {}

        # Detect rewritten history before persisting anything so a failed run
        # never advances the checkpoint of an already imported event.
        for event in events:
            known = imported.get(event.event_id)
            if known is not None and known != _record_digest(event.record):
                message = f"pi event {event.event_id!r} was rewritten after it was imported"
                raise ValueError(message)

        pending = dict(imported)
        for event in events:
            if event.event_id in imported:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status.value == "accepted":
                pending[event.event_id] = _record_digest(event.record)
            else:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={
                "version": CHECKPOINT_VERSION,
                "session_id": session_id,
                "events": pending,
            },
        )


def _imported_events(checkpoint: JsonValue | None) -> tuple[dict[str, str], str | None]:
    if not isinstance(checkpoint, dict):
        return {}, None
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        return {}, None
    session_id = checkpoint.get("session_id")
    imported = {str(event_id): digest for event_id, digest in events.items() if isinstance(digest, str)}
    return imported, session_id if isinstance(session_id, str) else None


def _record_digest(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decode_archive(content: bytes) -> str:
    try:
        return bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("pi session archive must be valid strict UTF-8") from error  # noqa: TRY003


def _parse_line(line: str) -> dict[str, JsonValue]:
    try:
        document: Any = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("pi session archive line is not valid JSON") from error  # noqa: TRY003
    if not isinstance(document, dict):
        raise ValueError("pi session archive records must be JSON objects")  # noqa: TRY003, TRY004
    return document


def _require_identity_part(field: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"pi {field} must be a non-empty string")  # noqa: TRY003


def _require_event(value: PiEventInput) -> None:
    _require_identity_part("session_id", value.session_id)
    _require_identity_part("event_id", value.event_id)
    if value.parent_id is not None:
        _require_identity_part("parent_id", value.parent_id)
    _require_identity_part("event_type", value.event_type)
    _require_identity_part("role", value.role)
    if not isinstance(value.content, str) or not value.content.strip():
        raise ValueError("pi event content must be a non-blank string")  # noqa: TRY003
    if not isinstance(value.record, dict):
        raise ValueError("pi event record must be the original JSON object")  # noqa: TRY003, TRY004


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_DEFINITION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


__all__ = [
    "PI_EVENT_DEFINITION_VERSION",
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_TYPE",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "compact_json",
    "parse_pi_session",
    "pi_event_source_id",
]
