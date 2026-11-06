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
_FOCUS_MAX_BYTES = 96
# Smallest body the excerpt may compete with: the full trust policy, the envelope, and one item
# carrying the minimum truncated content. A budget below this cannot hold both, so the excerpt
# is dropped rather than starving the item array that the protocol is actually measured on.
_FOCUS_MIN_BODY_BYTES = 660
# The same floor with the compact trust line, which is what a budget below the full floor gets.
_COMPACT_MIN_BODY_BYTES = 415
_FOCUS_FRAME_BYTES = 16
_FOCUS_TERM_MIN_LENGTH = 3
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
        compact = request.max_bytes < _FOCUS_MIN_BODY_BYTES
        room = max(0, request.max_bytes - (_COMPACT_MIN_BODY_BYTES if compact else _FOCUS_MIN_BODY_BYTES))
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
            cap=min(_FOCUS_MAX_BYTES, room - _FOCUS_FRAME_BYTES),
        )
        reserve = _focus_reserve(focus)
        if reserve > room:
            focus, reserve = None, 0
        entries, omissions = self._fit_entries(
            request,
            memory_entries,
            topic_memory_entries,
            experience_entries,
            reserve=reserve,
            compact=compact,
        )
        if not entries and focus is not None:
            focus = None
            entries, omissions = self._fit_entries(
                request,
                memory_entries,
                topic_memory_entries,
                experience_entries,
                compact=compact,
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
            )
            if not entries:
                return None, (), omissions, ()
        # Render in selection order, never in identity order.  Entry identities are random
        # uuids, so sorting by them permutes the body and drops every new Memory entry at a
        # random offset - an incremental delta then has to resend everything after it.  The
        # pooled candidates arrive in first-seen order, which only ever grows at the tail,
        # so keeping that order is what makes the body prefix-stable across rounds.
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
            if section.family in {"memory", "experience"}:
                # Rank decides which entries the budget can afford; identity decides where
                # they sit. Sorting the candidates outright would let a permuted body pick a
                # different survivor set whenever the budget is tight, so the layout is
                # applied to the selection instead of to the candidate order.
                layout = True
            else:
                layout = False
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
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        # ``reserve`` is the trailing byte count the renderer will add after this array (the
        # focus excerpt). Bounding the items by ``max_bytes - reserve`` keeps the finished body
        # inside the caller's budget without rendering it twice.
        ceiling = max(0, request.max_bytes - reserve)
        # Families are concatenated in a fixed order instead of interleaved: Memory
        # arrives in first-seen order (append-only across rounds), so every recalled
        # entry keeps its chunk position and an incremental delta only carries the tail.
        for candidate in (*memory_entries, *topic_memory_entries, *experience_entries):
            if len(entries) >= self.entry_limit:
                break
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
    """

    if cap <= 0:
        return None
    terms = _query_terms(query)
    if terms:
        best_text: str | None = None
        best_score = 0
        for entry in entries:
            lowered = entry.content.casefold()
            score = sum(1 for term in terms if term in lowered)
            if score > best_score:
                best_text, best_score = entry.content, score
        if best_text is not None:
            excerpt = _matching_span(best_text, terms, cap)
            if excerpt:
                return excerpt
    if fallback:
        return _truncate_utf8(fallback, cap) or None
    return None


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


def _rendered_bytes(
    entries: Sequence[_PreparedContextEntry],
    *,
    focus: str | None = None,
    compact: bool = False,
) -> int:
    return len(_render(entries, focus=focus, compact=compact).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"
