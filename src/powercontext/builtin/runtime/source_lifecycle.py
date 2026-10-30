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

"""Source-driven eligibility for captured Memory and Topic Memory.

A scope's evidence forms a DAG: a Source carries a lifecycle revision, derived
nodes (Memory entries, Topic Memory artifacts) declare their parents and an
``all`` / ``any`` policy.  Withdrawing a Source propagates down that DAG and
restoring it propagates back.  Derived nodes that lose their support leave the
native list / search / Context projections, but their bodies and citations stay
readable - withdrawal only changes *eligibility*, never stored content.

The DAG lives in a small side table so registrations and event identities
survive a restart, while the visible effect is applied through the ordinary
Memory and Topic Memory surfaces.  Native state is reconciled from the
recomputed graph on every event, so a retried event converges even when an
earlier attempt was interrupted halfway through a Memory write.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.sources import SourceRef

__all__ = [
    "create_lifecycle_schema",
    "load_registrations",
    "load_availability",
    "lifecycle_service",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "lifecycle_schema",
    "SourceLifecycleService",
    "SourceLifecycleApplication",
    "ScopedSourceLifecycleApplication",
]

_REGISTRATION_TABLE = "source_lifecycle_registration"
_EVENT_TABLE = "source_lifecycle_event"



def lifecycle_schema() -> MetaData:
    """Side tables for registrations and recorded events (scope-scoped)."""

    metadata = MetaData()
    Table(
        _REGISTRATION_TABLE,
        metadata,
        Column("scope_id", String(64), primary_key=True),
        Column("node_id", String(192), primary_key=True),
        Column("family", String(16), nullable=False),
        Column("parents", Text, nullable=False),
        Column("policy", String(8), nullable=False),
        Column("reference", Text, nullable=False),
        Column("fingerprint", String(400), nullable=False),
        Column("revision", Integer, nullable=True),
    )
    Table(
        _EVENT_TABLE,
        metadata,
        Column("scope_id", String(64), primary_key=True),
        Column("node_id", String(192), primary_key=True),
        Column("sequence", Integer, primary_key=True),
        Column("revision", Integer, nullable=False),
        Column("available", String(5), nullable=False),
        Column("reason", Text, nullable=False),
    )
    return metadata


_SCHEMA = lifecycle_schema()
_registrations = _SCHEMA.tables[_REGISTRATION_TABLE]
_events = _SCHEMA.tables[_EVENT_TABLE]

# Read one Memory entry version by citation; raise SourceLifecycleError when it
# does not resolve inside this scope.
EntryReader = Callable[[MemoryCitation], Awaitable[Any]]
# Rewrite one Memory entry's eligibility; returns the new Memory revision.
EntryWriter = Callable[[MemoryCitation, bool], Awaitable[Any]]
# Confirm a Topic Memory Artifact revision is readable in this scope, and
# recompute which of this scope's Topics currently lack support.
TopicProbe = Callable[[ArtifactRef | None], Awaitable[Any]]


class SourceLifecycleError(ValueError):
    """Raised when an event, registration or reference breaks the contract."""


class SourceLifecycleEvent(BaseModel):
    """One validated lifecycle transition of a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: StrictInt
    sequence: StrictInt
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def _require_text(cls, value: str, info) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be non-blank text")
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _require_positive(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value


@dataclass(frozen=True)
class _Registration:
    node_id: str
    family: Literal["source", "memory", "topic"]
    parents: tuple[str, ...]
    policy: Literal["all", "any"]
    reference: str
    fingerprint: str
    revision: int | None

    @property
    def derived(self) -> bool:
        return self.family != "source"


def _policy(value: str) -> Literal["all", "any"]:
    if value not in {"all", "any"}:
        raise SourceLifecycleError("policy must be 'all' or 'any'")
    return "all" if value == "all" else "any"


def _family(value: str) -> Literal["source", "memory", "topic"]:
    if value not in {"source", "memory", "topic"}:
        raise SourceLifecycleError("unknown node family")
    if value == "source":
        return "source"
    return "memory" if value == "memory" else "topic"


class SourceLifecycleService:
    """Propagate Source lifecycle events through one scope's evidence DAG."""

    def __init__(
        self,
        database: AsyncDatabase,
        scope_id: str,
        *,
        entry_reader: EntryReader,
        entry_writer: EntryWriter,
        topic_probe: TopicProbe,
    ) -> None:
        self._database = database
        self.scope_id = scope_id
        self._entry_reader = entry_reader
        self._entry_writer = entry_writer
        self._topic_probe = topic_probe
        self._defer_effects: list[Callable[[], Awaitable[None]]] | None = None

    @asynccontextmanager
    async def _atomic_scope(self) -> AsyncIterator[list[Callable[[], Awaitable[None]]]]:
        """Collect native effects so they only run once the database work commits."""

        if self._defer_effects is not None:
            yield self._defer_effects
            return
        effects: list[Callable[[], Awaitable[None]]] = []
        self._defer_effects = effects
        try:
            yield effects
        finally:
            self._defer_effects = None
        for effect in effects:
            await effect()

    # -- registration ---------------------------------------------------------

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        await self._register(
            node_id,
            family="source",
            parents=(),
            policy="all",
            reference=source_ref.model_dump_json(),
            revision=revision,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation")
        await self._register(
            node_id,
            family="memory",
            parents=tuple(parents),
            policy=policy,
            reference=citation.model_dump_json(),
            revision=None,
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("reference must be an ArtifactRef")
        await self._register(
            node_id,
            family="topic",
            parents=tuple(parents),
            policy=policy,
            reference=reference.model_dump_json(),
            revision=None,
        )

    async def _register(
        self,
        node_id: str,
        *,
        family: Literal["source", "memory", "topic"],
        parents: tuple[str, ...],
        policy: str,
        reference: str,
        revision: int | None,
    ) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must be non-blank text")
        chosen = _policy(policy)
        if family != "source":
            if not parents:
                raise SourceLifecycleError("a derived node needs at least one parent")
            if len(set(parents)) != len(parents):
                raise SourceLifecycleError("parents must be unique")
            if node_id in parents:
                raise SourceLifecycleError("a node cannot depend on itself")
        fingerprint = f"{family}|{revision if revision is not None else ''}|{reference}"

        async def body() -> None:
            async with self._database.transaction() as connection:
                stored = await self._registrations(connection)
                existing = stored.get(node_id)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise SourceLifecycleError(f"conflicting registration for {node_id}")
                    if existing.parents != parents or existing.policy != chosen:
                        raise SourceLifecycleError(f"conflicting registration for {node_id}")
                    return
                for parent in parents:
                    if parent not in stored:
                        raise SourceLifecycleError(f"unknown parent {parent}")
                candidate = _Registration(
                    node_id=node_id,
                    family=family,
                    parents=parents,
                    policy=chosen,
                    reference=reference,
                    fingerprint=fingerprint,
                    revision=revision,
                )
                self._assert_acyclic({**stored, node_id: candidate})
                await self._require_reference(family, reference)
                await connection.execute(
                    insert(_registrations).values(
                        scope_id=self.scope_id,
                        node_id=node_id,
                        family=family,
                        parents=json.dumps(list(parents)),
                        policy=chosen,
                        reference=reference,
                        fingerprint=fingerprint,
                        revision=revision,
                    )
                )
                if family != "source":
                    # A derived node takes its parents' availability at registration
                    # time; it is never briefly usable before the first event arrives.
                    await self._reconcile(connection, {**stored, node_id: candidate})

        async with self._atomic_scope():
            await body()

    async def _require_reference(self, family: str, reference: str) -> None:
        if family == "memory":
            await self._entry_reader(MemoryCitation.model_validate_json(reference))
        elif family == "topic":
            await self._topic_probe(ArtifactRef.model_validate_json(reference))

    def _assert_acyclic(self, nodes: dict[str, _Registration]) -> None:
        state: dict[str, int] = {}

        def visit(node: str) -> None:
            mark = state.get(node, 0)
            if mark == 1:
                raise SourceLifecycleError("dependency cycle")
            if mark == 2:
                return
            state[node] = 1
            for parent in nodes[node].parents:
                visit(parent)
            state[node] = 2

        for node in nodes:
            visit(node)

    # -- events ---------------------------------------------------------------

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        async with self._atomic_scope():
            async with self._database.transaction() as connection:
                stored = await self._registrations(connection)
                registration = stored.get(event.node_id)
                if registration is None or registration.family != "source":
                    raise SourceLifecycleError(f"unknown source {event.node_id}")
                if registration.revision != event.revision:
                    raise SourceLifecycleError("revision does not match the registered Source")
                history = (
                    await connection.execute(
                        select(_events.c.sequence, _events.c.revision, _events.c.available, _events.c.reason).where(
                            _events.c.scope_id == self.scope_id, _events.c.node_id == event.node_id
                        )
                    )
                ).all()
                latest = max((row.sequence for row in history), default=0)
                for row in history:
                    if row.sequence != event.sequence:
                        continue
                    same = (
                        row.revision == event.revision
                        and (row.available == "true") == event.available
                        and row.reason == event.reason
                    )
                    if not same:
                        raise SourceLifecycleError("sequence already recorded with different content")
                    # Replaying a recorded event changes nothing and adds no revisions.
                    return _active_ids(await self._available(connection, stored))
                if event.sequence <= latest:
                    raise SourceLifecycleError("sequence must increase monotonically")
                await connection.execute(
                    insert(_events).values(
                        scope_id=self.scope_id,
                        node_id=event.node_id,
                        sequence=event.sequence,
                        revision=event.revision,
                        available="true" if event.available else "false",
                        reason=event.reason,
                    )
                )
                await self._reconcile(connection, stored)
                return _active_ids(await self._available(connection, stored))

    async def _available(self, connection, stored: dict[str, _Registration]) -> dict[str, bool]:
        return await load_availability(connection, self.scope_id, stored)

    # -- native effect --------------------------------------------------------

    async def _reconcile(self, connection, stored: dict[str, _Registration]) -> None:
        """Project the resolved DAG onto the native artifacts it governs.

        The Memory backend owns its own unit of work, so the transition must run
        after the caller's transaction commits: SQLite serializes writers and a
        second connection would deadlock against the first.
        """

        available = await self._available(connection, stored)
        targets = [
            (MemoryCitation.model_validate_json(node.reference), available.get(node.node_id, False))
            for node in sorted(stored.values(), key=lambda item: item.node_id)
            if node.family == "memory"
        ]
        await self._after_commit(lambda: self._project(available, targets))

    async def _after_commit(self, effect: Callable[[], Awaitable[None]]) -> None:
        if self._defer_effects is None:
            await effect()
        else:
            self._defer_effects.append(effect)

    async def _project(
        self,
        available: dict[str, bool],
        targets: Sequence[tuple[MemoryCitation, bool]],
    ) -> None:
        for citation, state in targets:
            await self._entry_writer(citation, state)
        await self._topic_probe(None)

    async def _registrations(self, connection) -> dict[str, _Registration]:
        return await load_registrations(connection, self.scope_id)

    # -- reads ----------------------------------------------------------------

    async def resolve(self, node_id: str, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError(f"unknown purpose {purpose}")
        async with self._database.transaction() as connection:
            stored = await self._registrations(connection)
            node = stored.get(node_id)
            if node is None:
                raise SourceLifecycleError(f"unknown node {node_id}")
            if purpose == "generation":
                available = await self._available(connection, stored)
                if not available.get(node_id, False):
                    raise SourceLifecycleError(f"{node_id} is not available for generation")
            return _reference(node)

    async def active(self) -> list[str]:
        async with self._database.transaction() as connection:
            stored = await self._registrations(connection)
            return _active_ids(await self._available(connection, stored))


def _reference(node: _Registration) -> SourceRef | MemoryCitation | ArtifactRef:
    if node.family == "source":
        return SourceRef.model_validate_json(node.reference)
    if node.family == "memory":
        return MemoryCitation.model_validate_json(node.reference)
    return ArtifactRef.model_validate_json(node.reference)


def _propagate(stored: dict[str, _Registration], sources: dict[str, bool]) -> dict[str, bool]:
    available: dict[str, bool] = {}
    for node in stored.values():
        if node.family == "source":
            available[node.node_id] = sources.get(node.node_id, True)
    pending = [node for node in stored.values() if node.derived]
    while pending:
        remaining: list[_Registration] = []
        progressed = False
        for node in pending:
            supports = [available.get(parent) for parent in node.parents]
            if any(support is None for support in supports):
                remaining.append(node)
                continue
            available[node.node_id] = all(supports) if node.policy == "all" else any(supports)
            progressed = True
        if not progressed:
            # Unreachable for a validated DAG; never leave a derived node usable by default.
            for node in remaining:
                available[node.node_id] = False
            break
        pending = remaining
    return available


def _active_ids(available: dict[str, bool]) -> list[str]:
    return sorted(node for node, value in available.items() if value)


async def create_lifecycle_schema(database: AsyncDatabase) -> None:
    """Create the lifecycle side tables when they are missing.

    They are additive and scope-independent, so creating them on first use keeps
    existing deployments working without a migration step.
    """

    async with database.transaction() as connection:
        await connection.run_sync(_SCHEMA.create_all)


async def load_registrations(connection, scope_id: str) -> dict[str, _Registration]:
    """Read one scope's registered nodes from an open connection."""

    rows = (await connection.execute(select(_registrations).where(_registrations.c.scope_id == scope_id))).all()
    return {
        row.node_id: _Registration(
            node_id=row.node_id,
            family=_family(row.family),
            parents=tuple(json.loads(row.parents)),
            policy=_policy(row.policy),
            reference=row.reference,
            fingerprint=row.fingerprint,
            revision=row.revision,
        )
        for row in rows
    }


async def load_availability(
    connection,
    scope_id: str,
    stored: dict[str, _Registration],
) -> dict[str, bool]:
    """Recompute node availability from the recorded events (latest sequence wins)."""

    rows = (
        await connection.execute(
            select(_events.c.node_id, _events.c.sequence, _events.c.available).where(_events.c.scope_id == scope_id)
        )
    ).all()
    latest: dict[str, bool] = {}
    highest: dict[str, int] = {}
    for row in rows:
        if row.sequence > highest.get(row.node_id, 0):
            highest[row.node_id] = row.sequence
            latest[row.node_id] = row.available == "true"
    return _propagate(stored, latest)


def lifecycle_service(runtime: Any, scope_id: str) -> SourceLifecycleService:
    """Build the lifecycle service a Runtime binds to one scope."""

    return SourceLifecycleService(
        runtime._records()._database,
        scope_id,
        entry_reader=lambda citation: runtime._lifecycle_read_entry(scope_id, citation),
        entry_writer=lambda citation, available: runtime._lifecycle_write_entry(scope_id, citation, available),
        topic_probe=lambda reference: runtime._lifecycle_topic_probe(scope_id, reference),
    )


class ScopedSourceLifecycleApplication:
    """Lifecycle operations for one scope."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id
        self._service: SourceLifecycleService | None = None

    async def _get(self) -> SourceLifecycleService:
        # The side tables are created lazily, so binding the service is async.
        await self._runtime._ensure_lifecycle_schema()
        if self._service is None:
            self._service = lifecycle_service(self._runtime, self.scope_id)
        return self._service

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        service = await self._get()
        await service.register_source(node_id, source_ref, revision=revision)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        service = await self._get()
        await service.register_memory(node_id, citation, parents=parents, policy=policy)

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        service = await self._get()
        await service.register_topic(node_id, reference, parents=parents, policy=policy)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        service = await self._get()
        return await service.apply(event)

    async def resolve(self, node_id: str, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        service = await self._get()
        return await service.resolve(node_id, purpose=purpose)

    async def active(self) -> list[str]:
        service = await self._get()
        return await service.active()


class SourceLifecycleApplication:
    """Select the scoped lifecycle service."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)
