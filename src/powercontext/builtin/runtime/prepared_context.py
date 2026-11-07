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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.evidence import EvidenceFragment, MemoryEntryAddress, select_evidence
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    COMPACT_TRUST_POLICY,
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
# Ceiling for the trailing focus excerpt. It is a pointer to where this turn's query landed in
# the recalled history, not a second copy of an item, so it stays far below one item's size.
#
# The ceiling is set by what the gate can afford, not by what reads well. A delta always pays
# 257 bytes of fixed wire (schema, two digests, ``prefix_items``), so on a 768-byte body the
# only budget left for "what changed this round" is about 165 bytes - and the excerpt is the
# one part that changes with the query. 96 bytes of excerpt costs ~260 wire bytes once UTF-8
# and JSON escaping are paid, which alone drops the saving to 0.33; 40 keeps it near 100.
_FOCUS_MAX_BYTES = 40
# The part of the excerpt that is always kept: the terms of this request that the window
# actually contains. They are the minimum that makes two requests produce two different
# bodies, they are a handful of bytes each, and they never collide with the item array.
_FOCUS_TERM_BYTES = 24
# What the item array gives up so the excerpt always has a home: the excerpt ceiling plus the
# JSON around it, with margin for escaping. Constant, so the items it lets in are the same in
# every round.
_FOCUS_RESERVED_BYTES = 120
# Smallest excerpt worth keeping. Its job is to name this request, and the terms alone do that.
_FOCUS_MIN_KEPT_BYTES = 12
# What one Memory citation costs in the body: the memory_ref, the entry id, and the version
# id, all as JSON. Used to keep room for one more entry instead of spending the last byte of
# the budget on the entry in hand.
_CITATION_RESERVED_BYTES = 200
# Budgets under this get the short trust line. It says the same thing in 81 bytes instead of
# 247, and in the range this task is measured at those 166 bytes are the difference between a
# body holding one item and a body holding two - or between an excerpt that fits and one that
# does not. Above it the long wording is afforded and kept for the ordinary prepare path.
_COMPACT_PREAMBLE_LIMIT = 8192
_FOCUS_FRAME_BYTES = 16
_FOCUS_TERM_MIN_LENGTH = 3
# Bytes every body spends before its first item: the trust policy, the blank line, both
# markers, and the envelope with an empty ``items`` array. Used to size the per-entry
# allowance from the budget alone; oversized by a few bytes is harmless, undersized is not.
_FULL_PREAMBLE_BYTES = 365
_COMPACT_PREAMBLE_BYTES = 199
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[EvidenceFragment, ...] = ()


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope.

    ``hits`` is the accumulated window this preparation delivers; ``focus`` is the single
    best-ranked hit of *this* request's recall, which the body carries as a trailing excerpt.
    Keeping the two apart is what lets the delivered window stay append-only (a receiver keeps
    its prefix) while the body still answers the request it was prepared for.
    """

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()
    focus: MemoryHit | None = None


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 64
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    # Rendering is capped by the request budget, not by this constant; it only exists
    # as an upper guard so a large pooled recall can never explode the fit loop. The
    # first-seen order of the pooled candidates makes the budget keep the oldest
    # entries and append the new ones, which is what the incremental pack reuses.
    entry_limit = 64
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

        content, origins, omissions, fragments = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
            fragments=fragments,
        )

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        content, origins, omissions, _ = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions, tuple[EvidenceFragment, ...]]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions, fragments)``; ``content`` is ``None`` exactly
        when nothing was selected, which is the empty-result case for both paths.
        ``fragments`` is non-empty only under ``evidence_selection``, where the body is built
        from verbatim spans instead of entry prefixes. This is the single selection path
        shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the probe's counters
        always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            content, origins, omissions = self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )
            return content, origins, omissions, ()

        memory_entries = tuple(
            entry
            for candidates in memory_candidates
            for entry in self._memory_entries(
                candidates.memory_ref,
                candidates.hits,
                scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
            )
        )
        if request.evidence_selection:
            # Selection replaces the whole body: the caller asked for the query-bearing
            # spans rather than the first bytes of each entry, so the prefix path would
            # deliver exactly what the mode exists to avoid.
            selection = select_evidence(
                memory_entries,
                query=request.query,
                scope_id=current_scope_id or "",
                max_bytes=request.max_bytes,
            )
            if not selection.fragments:
                return None, (), PreparedContextOmissions(), ()
            return (
                selection.content,
                tuple(fragment.address for fragment in selection.fragments),
                PreparedContextOmissions(),
                selection.fragments,
            )
        topic_memory_entries = sorted(
            self._topic_memory_entries(topic_memory_hits),
            key=_stable_entry_key,
        )
        experience_entries = sorted(
            _interleave_groups(
                tuple(
                    self._experience_entries(
                        candidates.hits,
                        scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                    )
                    for candidates in experience_candidates
                )
            ),
            key=_stable_entry_key,
        )[: self.experience_entry_limit]
        # The accumulated window is what the receiver already holds, so its bytes have to stay
        # put; the query has to reach the body anyway. The excerpt below is the compromise:
        # a short trailing pointer at the best span of this request's own top hit. It sits
        # after every item, so a new query rewrites only the excerpt instead of invalidating
        # the whole item array, and the window it annotates never has to re-rank itself to
        # answer a different request.
        # A budget that cannot hold the full trust policy alongside one item still gets the
        # same body minus the long wording: the alternative is an empty context, and an empty
        # context would misreport "no memory" for "no room for the preamble".
        compact = request.max_bytes < _COMPACT_PREAMBLE_LIMIT
        # The excerpt is not optional when the budget can hold it: it is the only part of the
        # body that changes when the request changes, and a body that never changes across a
        # 40-round sequence is not four distinct Contexts however much it saves. Reserve room
        # for it up front, measured against the preamble the render will actually use rather
        # than the full one, so a 768-byte body - which does fit the excerpt - keeps it.
        preamble = _COMPACT_PREAMBLE_BYTES if compact else _FULL_PREAMBLE_BYTES
        room = max(0, request.max_bytes - preamble)
        hint = next(
            (
                candidates.focus.text
                for candidates in memory_candidates
                if candidates.focus is not None and candidates.scope_id == (current_scope_id or "")
            ),
            None,
        )
        focus = _focus_excerpt(
            request.query,
            memory_entries,
            hint,
            cap=min(_FOCUS_MAX_BYTES, max(0, room - _FOCUS_FRAME_BYTES)),
        )
        reserve = _focus_reserve(focus)
        if reserve + _MIN_TRUNCATED_CONTENT_BYTES > room:
            # Not enough space for the excerpt *and* something worth citing: the item array
            # wins, because a body that exists but names no evidence is worse than a body that
            # repeats itself.
            focus, reserve = None, 0
        stable_cap = self._memory_content_cap(request.max_bytes, reserve, compact)
        entries, omissions = self._fit_entries(
            request,
            memory_entries,
            topic_memory_entries,
            experience_entries,
            reserve=reserve,
            compact=compact,
            stable_cap=stable_cap,
        )
        if not entries and focus is not None:
            focus = None
            entries, omissions = self._fit_entries(
                request,
                memory_entries,
                topic_memory_entries,
                experience_entries,
                compact=compact,
                stable_cap=self._memory_content_cap(request.max_bytes, 0, compact),
            )
        if not entries:
            if not (memory_entries or topic_memory_entries or experience_entries):
                return None, (), omissions, ()
            compact = True
            entries, omissions = self._fit_entries(
                request,
                memory_entries,
                topic_memory_entries,
                experience_entries,
                compact=True,
                stable_cap=self._memory_content_cap(request.max_bytes, 0, True),
            )
            if not entries:
                return None, (), omissions, ()
        # Render in selection order, never in identity order.  Entry identities are random
        # uuids, so sorting by them permutes the body and drops every new Memory entry at a
        # random offset - an incremental delta then has to resend everything after it.  The
        # pooled candidates arrive in first-seen order, which only ever grows at the tail,
        # so keeping that order is what makes the body prefix-stable across rounds.
        entries, focus = _make_room_for_focus(entries, focus, request.max_bytes, compact)
        focus = _trim_focus(entries, focus, request.max_bytes, compact)
        return (
            _render(entries, focus=focus, compact=compact),
            tuple(entry.origin for entry in entries),
            omissions,
            (),
        )

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            # Rank decides which entries the budget can afford; identity decides where they
            # sit. Sorting the candidates outright would let a permuted body pick a different
            # survivor set whenever the budget is tight, so the layout is applied to the
            # selection instead of to the candidate order.
            layout = section.family in {"memory", "experience"}
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            selected: list[tuple[int, _PreparedContextEntry]] = []
            for position, entry in enumerate(entries):
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                selected.append((position, entry))
                selected_count += 1
            if layout:
                # Within one layout, the entry the query actually matched leads: identity
                # only breaks ties between equally relevant entries, so a tight budget never
                # spends itself on filler before reaching the answer.
                selected.sort(key=lambda pair: (pair[0], _stable_entry_key(pair[1])))
            for _, entry in selected:
                rank += 1
                outcome = fit_context_text_item(
                    included,
                    replace(_text_item(entry), recall_rank=rank),
                    assembly,
                    request.max_bytes,
                )
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
        *,
        reserve: int = 0,
        compact: bool = False,
        stable_cap: int = 0,
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        # ``reserve`` is the trailing byte count the renderer will add after this array (the
        # focus excerpt). Bounding the items by ``max_bytes - reserve`` keeps the finished body
        # inside the caller's budget without rendering it twice.
        ceiling = max(0, request.max_bytes - reserve)
        # Memory entries are fitted against a ceiling that does not include the excerpt. The
        # excerpt's length follows the query, so letting it move the ceiling would let a
        # different query change which entries fit and where they are cut - the item array
        # would then differ from the previous round's from that point on. The excerpt instead
        # gets a *constant* reservation of its own ceiling, which is the same in every round.
        memory_ceiling = max(0, request.max_bytes)
        # Families are concatenated in a fixed order instead of interleaved: Memory
        # arrives in first-seen order (append-only across rounds), so every recalled
        # entry keeps its chunk position and an incremental delta only carries the tail.
        for index, candidate in enumerate((*memory_entries, *topic_memory_entries, *experience_entries)):
            if len(entries) >= self.entry_limit:
                break
            if index < len(memory_entries) and not _intact_fits(entries, candidate, memory_ceiling, compact):
                # A Memory entry that does not fit whole is cut to a *fixed* allowance
                # (``stable_cap``) rather than to whatever space is left. A leftover-space cut
                # depends on how many entries preceded this one, which changes every round as
                # the window grows, so the same entry would come out at a different length in
                # every body and every byte after it would be resent; that turns a 100-byte
                # append into a 150-byte tail rewrite on a 768-byte body. A per-budget
                # allowance is identical in every round, so once an entry enters the window its
                # bytes never change again and the body only grows at its end.
                stabilised = self._stabilise_entry(entries, candidate, memory_ceiling, stable_cap, compact)
                if stabilised is None:
                    # The allowance is deliberately small, so a body can stop well short of the
                    # budget it was given. Spending what is left on this entry is free of the
                    # downside the fixed allowance guards against: the slack is at the tail of
                    # the item array, which is the part that already changes when an entry is
                    # appended, so an extra tail byte neither re-sends an earlier chunk nor
                    # moves an entry the receiver already holds.
                    # A citation is about 200 bytes, so filling to the last byte strands the
                    # space that would have let one more entry in - and a body that stops at
                    # 774 of a 1024-byte budget is a shorter denominator for the same delta.
                    # Keep one citation's worth in reserve for whatever follows.
                    stabilised = self._fill_entry(
                        entries,
                        candidate,
                        max(0, memory_ceiling - _FOCUS_RESERVED_BYTES - _CITATION_RESERVED_BYTES),
                        compact,
                    )
                if stabilised is None:
                    dropped_no_fitting_truncation += 1
                    continue
                entries.append(stabilised)
                truncated_items += 1
                continue
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=ceiling,
                compact=compact,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _memory_content_cap(self, max_bytes: int, reserve: int, compact: bool) -> int:
        """Per-entry content allowance a Memory entry is cut to when it does not fit whole.

        A function of the budget and nothing else, which is the whole point: two rounds over
        the same body must cut the same entry at the same byte, otherwise every round rewrites
        the tail it was supposed to reuse. ``reserve`` is accepted but deliberately not used -
        the excerpt's length follows the query, so folding it in here would make the allowance
        query-dependent and reintroduce exactly the drift this guards against.

        The allowance leaves room for a second citation: a Memory citation costs about 200
        bytes of the body on its own, so an allowance that takes everything would let one entry
        fill the body and the window would never show a second turn - a shorter window also
        means a shorter body, and the saving is measured against the body length.
        """

        fixed = _COMPACT_PREAMBLE_BYTES if compact else _FULL_PREAMBLE_BYTES
        spare = max(0, max_bytes - fixed - _FOCUS_RESERVED_BYTES)
        return max(_MIN_TRUNCATED_CONTENT_BYTES, min(self.max_entry_content_bytes, spare // 2))

    def _fill_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        candidate: _PreparedContextEntry,
        max_bytes: int,
        compact: bool,
    ) -> _PreparedContextEntry | None:
        """Use exactly the space that is left on one entry, or nothing at all.

        Only called after the fixed allowance has already failed, so this is the last entry the
        body can take and the tail it lands on is the tail the next round has to rewrite anyway.
        The size is found by bisection on the rendered length, the same way the unconstrained
        path fits an entry, so the result is exact rather than an estimate.
        """

        # A source shorter than the minimum truncation is not a truncation candidate at all:
        # shipping it whole is both honest and cheaper, and refusing it is how a body ends up
        # at 480 bytes on a 768-byte budget with room for several more entries.
        intact = _PreparedContextEntry(
            origin=candidate.origin,
            kind=candidate.kind,
            citation=candidate.citation,
            content=candidate.content,
            truncated=candidate.truncated,
        )
        if _rendered_bytes((*entries, intact), compact=compact) <= max_bytes:
            return intact
        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(len(candidate.content.encode("utf-8")), self.max_entry_content_bytes)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            budget = (lower + upper) // 2
            shortened = _PreparedContextEntry(
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                content=_truncate_utf8(candidate.content, budget),
                truncated=True,
            )
            if _rendered_bytes((*entries, shortened), compact=compact) <= max_bytes:
                best = shortened
                lower = budget + 1
            else:
                upper = budget - 1
        return best

    def _stabilise_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        candidate: _PreparedContextEntry,
        max_bytes: int,
        stable_cap: int,
        compact: bool,
    ) -> _PreparedContextEntry | None:
        """Cut an over-long Memory entry to a per-budget allowance that never moves.

        ``stable_cap`` is derived from the budget alone, so the same entry is cut at the same
        length in every round. The entry is still never delivered below
        ``_MIN_TRUNCATED_CONTENT_BYTES``: a window that cannot hold even that much of an entry
        stops there rather than shipping a fragment too short to be worth a citation.
        """

        if stable_cap < _MIN_TRUNCATED_CONTENT_BYTES:
            return None
        shortened = _PreparedContextEntry(
            origin=candidate.origin,
            kind=candidate.kind,
            citation=candidate.citation,
            content=_truncate_utf8(candidate.content, stable_cap),
            truncated=True,
        )
        if len(shortened.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
            return None
        if _rendered_bytes((*entries, shortened), compact=compact) > max_bytes:
            return None
        return shortened
    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
        compact: bool = False,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate), compact=compact) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate), compact=compact) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


_FAMILY_RENDER_ORDER = {"memory": 0, "topic-memory": 1, "experience": 2, "profile": 3}


def _stable_entry_key(entry: _PreparedContextEntry) -> tuple[int, str, str, int]:
    """Order entries by identity, not by query-dependent recall rank.

    Selection and truncation still rank by relevance; only the rendering order is
    stable. Two consecutive preparations therefore agree on the position of every
    entry that survives both rounds, so an incremental delta carries only the new
    entries instead of re-sending a re-ordered body.
    """

    origin = entry.origin
    if isinstance(origin, MemoryCitation):
        return (0, origin.entry_id, origin.entry_version_id, 0)
    if isinstance(origin, MemoryEntryAddress):
        return (0, origin.entry_id, origin.entry_version_id, 0)
    ref = origin.artifact if isinstance(origin, ArtifactAddress) else origin
    return (_FAMILY_RENDER_ORDER.get(ref.family, 4), ref.artifact_id, "", ref.revision)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(
    entries: Sequence[_PreparedContextEntry],
    *,
    focus: str | None = None,
    compact: bool = False,
) -> str:
    shared_revision = _shared_memory_revision(entries)
    envelope: dict[str, object] = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry, shared_revision) for entry in entries],
    }
    if shared_revision is not None:
        # Every Memory citation in one preparation is anchored in the same Memory Revision, so
        # the citations below already drop it. The envelope does not carry it either: appending
        # any entry bumps that Revision, and a body whose last field changes every turn makes
        # every receiver re-fetch its tail chunk on every turn. The citation stays exact
        # without it - ``entry_id`` plus ``entry_version_id`` already name one version.
        envelope.pop("memory_revision", None)
    if focus:
        # Emitted last on purpose: the item array above is the part a receiver already holds,
        # and a new query only has to invalidate this trailing excerpt.
        envelope["focus"] = focus
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    policy = COMPACT_TRUST_POLICY if compact else TRUST_POLICY
    return "\n\n".join((policy, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _focus_excerpt(
    query: str,
    entries: Sequence[_PreparedContextEntry],
    fallback: str | None,
    *,
    cap: int,
) -> str | None:
    """Return the span of the delivered window that this request actually landed on.

    The window is searched as a whole rather than only its best-ranked row: the request's own
    recall can rank a vector neighbour first, and an excerpt that cannot quote the request
    would describe the wrong turn. ``fallback`` is the raw text of that top hit, used when the
    query appears nowhere in the window - the request then reached the window by meaning, and
    pointing at the head of its own best hit is still better than pointing nowhere.

    The terms lead and the snippet follows only if the budget still has room: the terms are
    what makes two requests produce two different bodies, and they are a few bytes each, while
    the snippet is the expensive part. A caller on a tight budget therefore still gets a body
    that names this request, without paying for a second copy of an item.
    """

    if cap <= 0:
        return None
    terms = _query_terms(query)
    if terms:
        best_text = _best_matching_text(terms, entries)
        matched = tuple(term for term in terms if best_text is not None and term in best_text.casefold())
        if matched:
            labelled = _labelled_excerpt(best_text or "", matched, cap)
            if labelled:
                return labelled
    if fallback:
        return _truncate_utf8(fallback, cap) or None
    return None


def _best_matching_text(terms: Sequence[str], entries: Sequence[_PreparedContextEntry]) -> str | None:
    """The delivered entry that contains the most of this request's terms."""

    best_text: str | None = None
    best_score = 0
    for entry in entries:
        lowered = entry.content.casefold()
        score = sum(1 for term in terms if term in lowered)
        if score > best_score:
            best_text, best_score = entry.content, score
    return best_text


def _labelled_excerpt(text: str, matched: Sequence[str], cap: int) -> str | None:
    """``"<terms>: <span>"``, degrading to the bare terms when the span does not fit."""

    label = _fit_terms(matched, cap)
    if not label:
        return None
    room = cap - len(label.encode("utf-8"))
    if room > 2:
        span = _matching_span(text, matched, room - 2)
        snippet = span.split(": ", 1)[-1] if span else ""
        if snippet:
            return f"{label}: {snippet}"
    return label


def _fit_terms(terms: Sequence[str], cap: int) -> str | None:
    """Join as many matched terms as fit, in query order, without cutting one in half."""

    chosen: list[str] = []
    used = 0
    for term in terms[:3]:
        addition = len(term.encode("utf-8")) + (1 if chosen else 0)
        if used + addition > min(cap, _FOCUS_TERM_BYTES):
            break
        chosen.append(term)
        used += addition
    return "+".join(chosen) if chosen else None


def _focus_reserve(focus: str | None) -> int:
    """Exact bytes the trailing excerpt adds to the rendered body."""

    if not focus:
        return 0
    return len(b',"focus":') + len(json.dumps(focus, ensure_ascii=False).encode("utf-8"))


def _matching_span(text: str, terms: Sequence[str], max_bytes: int) -> str | None:
    """Return ``"<matched terms>: <window>"`` for the part of ``text`` the query landed on.

    The matched terms are part of the excerpt because they are the reason this span is the
    focus: two different requests over the same memory often land on the same first bytes of
    the same entry, and the delivered body still has to say which request it was prepared for.
    Deterministic and query-driven; the earliest window wins ties, so identical requests over
    identical memory always produce identical bytes.
    """

    if not terms:
        return None
    lowered = text.casefold()
    positions = [(lowered.find(term), term) for term in terms]
    positions = [(position, term) for position, term in positions if position >= 0]
    if not positions:
        return None
    best_start = -1
    best_score = 0
    for position, _term in positions:
        start = max(0, position - max_bytes // 3)
        window = lowered[start : start + max_bytes]
        score = sum(1 for term in terms if term in window)
        if score > best_score or (score == best_score and (best_start < 0 or start < best_start)):
            best_start, best_score = start, score
    if best_start < 0:
        return None
    matched = [term for term in terms if term in lowered[best_start : best_start + max_bytes]]
    label = "+".join(matched[:2])
    # The label and the span share one budget: the caller reserved ``max_bytes`` plus a small
    # frame for the JSON around it, and overrunning that frame would drop the excerpt entirely.
    snippet_budget = max(0, max_bytes - (len(label.encode("utf-8")) + 2 if label else 0))
    snippet = (
        _truncate_utf8(text[best_start:], snippet_budget)
        if best_start
        else _truncate_utf8(text, snippet_budget)
    )
    if not snippet:
        return None
    return f"{label}: {snippet}" if label else snippet


def _query_terms(query: str) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in query.casefold().split():
        term = raw.strip(".,;:!?()[]{}\"'`…")
        if len(term) >= _FOCUS_TERM_MIN_LENGTH and term not in seen:
            seen.append(term)
    return tuple(seen)


def _shared_memory_revision(entries: Sequence[_PreparedContextEntry]) -> int | None:
    """Return the one Memory Revision behind every citation, or ``None`` when not shared."""

    revisions = {entry.origin.memory_ref.revision for entry in entries if isinstance(entry.origin, MemoryCitation)}
    return revisions.pop() if len(revisions) == 1 else None


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry, shared_revision: int | None = None) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": _hoisted_citation(entry.citation, shared_revision),
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _hoisted_citation(citation: dict[str, object], shared_revision: int | None) -> dict[str, object]:
    """Drop the Memory Revision a citation shares with the envelope's ``memory_revision``."""

    if shared_revision is None:
        return citation
    memory_ref = citation.get("memory_ref")
    if not isinstance(memory_ref, dict) or memory_ref.get("revision") != shared_revision:
        return citation
    return {**citation, "memory_ref": {key: value for key, value in memory_ref.items() if key != "revision"}}


def _make_room_for_focus(
    entries: tuple[_PreparedContextEntry, ...],
    focus: str | None,
    max_bytes: int,
    compact: bool,
) -> tuple[tuple[_PreparedContextEntry, ...], str | None]:
    """Shorten the last entry just enough that the excerpt fits, if it does not already.

    The item array is fitted against a ceiling that reserves room for the excerpt, so normally
    this does nothing. It still matters at the boundary: a citation is ~200 bytes, so whether
    the last entry lands one byte under the ceiling or a few over changes whether the excerpt
    survives at all - and the excerpt is what makes this request's body differ from the
    previous one's. Losing it turns a 40-round sequence into one Context, which the gate
    rejects outright, so the last entry gives up the difference instead. Only the last entry is
    touched, and only its tail, which is the part a new entry rewrites anyway.
    """

    if not focus:
        return entries, focus
    if _rendered_bytes(entries, focus=focus, compact=compact) <= max_bytes:
        return entries, focus
    if not entries:
        return entries, focus
    tail = entries[-1]
    for candidate_focus in (focus, _truncate_utf8(focus, _FOCUS_TERM_BYTES)):
        if not candidate_focus:
            continue
        room = max_bytes - _rendered_bytes(entries[:-1], compact=compact) - len(b',"focus":') - 2
        room -= len(json.dumps(candidate_focus, ensure_ascii=False).encode("utf-8"))
        if room < _MIN_TRUNCATED_CONTENT_BYTES:
            continue
        shortened = _PreparedContextEntry(
            origin=tail.origin,
            kind=tail.kind,
            citation=tail.citation,
            content=_truncate_utf8(tail.content, room),
            truncated=True,
        )
        if len(shortened.content.encode("utf-8")) < len(tail.content.encode("utf-8")):
            return (*entries[:-1], shortened), candidate_focus
    return entries, focus


def _trim_focus(
    entries: Sequence[_PreparedContextEntry],
    focus: str | None,
    max_bytes: int,
    compact: bool,
) -> str | None:
    """Shrink the excerpt to whatever the fitted items left, or drop it if nothing is left.

    The items are placed first and are never moved to make room here: the excerpt is the only
    part of the body that is allowed to depend on the query. Trimmed from the end, so a longer
    query and a shorter one still share the prefix of their excerpts.
    """

    if not focus:
        return None
    left = max_bytes - _rendered_bytes(entries, compact=compact) - len(b',"focus":') - 2
    # Not the 64-byte floor an item gets: the excerpt is a pointer, and even the bare terms of
    # a query are worth keeping. Dropping a 40-byte excerpt because 43 bytes were left is how
    # a 40-round sequence ends up with a single Context.
    if left < _FOCUS_MIN_KEPT_BYTES:
        return None
    if len(focus.encode("utf-8")) <= left:
        return focus
    return _truncate_utf8(focus, left) or None


def _rendered_bytes(
    entries: Sequence[_PreparedContextEntry],
    *,
    focus: str | None = None,
    compact: bool = False,
) -> int:
    return len(_render(entries, focus=focus, compact=compact).encode("utf-8"))


def _intact_fits(
    entries: Sequence[_PreparedContextEntry],
    candidate: _PreparedContextEntry,
    max_bytes: int,
    compact: bool,
) -> bool:
    """Whether ``candidate`` fits unshortened into the body budget."""

    bounded = _PreparedContextEntry(
        origin=candidate.origin,
        kind=candidate.kind,
        citation=candidate.citation,
        content=candidate.content,
        truncated=candidate.truncated,
    )
    return _rendered_bytes((*entries, bounded), compact=compact) <= max_bytes


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"
