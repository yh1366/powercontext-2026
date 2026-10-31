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

"""Query-driven evidence selection over already-recalled Memory entries.

A recovered Memory entry can be mostly boilerplate with one sentence that answers
the query.  Prefix truncation then delivers the boilerplate and loses the answer,
so when a caller opts into ``evidence_selection`` the body is rebuilt from
verbatim spans chosen for this query instead.

Selection never rewrites text: every fragment is ``entry.text[start:end]`` of a
real entry, and its citation keeps pointing at that exact entry version.  The
budget that decides how many fragments fit is the *rendered* UTF-8 size of the
whole envelope, so the accounting includes trust policy, markers, JSON keys and
citations - not just the bodies.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from typing import Any

from powercontext.artifacts import ArtifactAddress, ArtifactRef

from powercontext.builtin.runtime.prepared_text import _BEGIN_MARKER, _END_MARKER, TRUST_POLICY

__all__ = [
    "EvidenceFragment",
    "EvidenceSelection",
    "MemoryEntryAddress",
    "select_evidence",
]


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class EvidenceFragment:
    """One verbatim span of a Memory entry, selected for the current query."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str

_TOKEN = re.compile(r"[a-z]+|\d+")
# A sentence is the unit of selection: keeping whole sentences makes the delivered
# evidence readable and lets one entry yield several disjoint spans.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_MAX_FRAGMENTS = 24
# Sentence-level BM25.  The document frequency of a query term is counted over the
# candidate sentences rather than over the few candidate entries, because "appears
# in two of three abstracts" separates nothing while "appears in two of forty
# sentences" does.
_BM25_K1 = 1.2
_BM25_B = 0.6
# A sentence in an entry that matches the query as a whole is a better witness than
# the same words in an entry that merely shares one word, so entries contribute a
# bounded prior instead of competing on equal terms.
_ENTRY_PRIOR_WEIGHT = 0.35
# Greedy selection rewards breadth: a sentence covering a query term that nothing
# selected so far covers is worth more than another sentence reusing a covered one.
_COVERAGE_WEIGHT = 0.6


@dataclass(frozen=True)
class EvidenceSelection:
    """One prepared body rebuilt from verbatim fragments."""

    content: str | None
    fragments: tuple[EvidenceFragment, ...]


def _terms(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return non-overlapping ``(start, end)`` spans that tile ``text``."""

    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        start, end = cursor, match.start()
        if end > start and text[start:end].strip():
            spans.append((start, end))
        cursor = match.end()
    if cursor < len(text) and text[cursor:].strip():
        spans.append((cursor, len(text)))
    return spans


def _idf(query_terms: Sequence[str], corpus: Sequence[Counter[str]]) -> dict[str, float]:
    """Weight query terms by how rare they are across the candidate sentences.

    A term that every sentence repeats ("the", "cells") cannot tell evidence apart,
    so weighting by rarity is what lets a single distinctive word in the tail of a
    long document win the budget.  Counting over sentences instead of over the three
    candidate entries is what gives the weights any resolution to work with.
    """

    total = max(1, len(corpus))
    weights: dict[str, float] = {}
    for term in query_terms:
        containing = sum(1 for counts in corpus if term in counts)
        weights[term] = math.log(1.0 + (total - containing + 0.5) / (containing + 0.5))
    return weights


def _sentence_score(
    counts: Counter[str],
    length: int,
    weights: dict[str, float],
    average_length: float,
    prior: float,
) -> float:
    """Score one sentence the way BM25 scores a document, then apply the entry prior."""

    body = 0.0
    for term, weight in weights.items():
        frequency = counts.get(term, 0)
        if not frequency:
            continue
        saturation = frequency * (_BM25_K1 + 1.0) / (
            frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / average_length)
        )
        body += weight * saturation
    if body <= 0.0:
        return 0.0
    # The prior is bounded to [1-w, 1], so it can order two equally good sentences but
    # never lets a barely relevant entry outrank a sentence that actually carries terms.
    return body * (1.0 - _ENTRY_PRIOR_WEIGHT * (1.0 - prior))


def _render(fragments: Sequence[EvidenceFragment], *, width: int | None = None) -> str:
    """Render fragments into the evidence envelope.

    ``width`` keeps at most that many leading characters of each fragment.  The
    caller uses it to bisect for the largest body set the byte budget can carry,
    and the truncation happens here so the surviving spans stay contiguous
    prefixes of their originals - never mid-character rewrites.
    """

    from powercontext.builtin.runtime.prepared_text import TRUST_POLICY, _BEGIN_MARKER, _END_MARKER

    items = []
    for fragment in fragments:
        address = fragment.address
        items.append(
            {
                "citation": {
                    "scope_id": address.memory.scope_id,
                    "memory_ref": address.memory.artifact.model_dump(mode="json"),
                    "entry_id": address.entry_id,
                    "entry_version_id": address.entry_version_id,
                },
                "content": fragment.text if width is None else fragment.text[:width],
                "start": fragment.start,
                "end": fragment.end if width is None else fragment.start + min(width, len(fragment.text)),
            }
        )
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _fits(fragments: Sequence[EvidenceFragment], max_bytes: int) -> bool:
    return len(_render(fragments).encode("utf-8")) <= max_bytes


def _pack_to_budget(fragments: Sequence[EvidenceFragment], max_bytes: int) -> tuple[EvidenceFragment, ...]:
    """Keep every fragment the budget can still carry, in selection order.

    Dropping the tail wholesale would throw away a short decisive sentence only
    because a long one above it did not fit, so a fragment that overflows is skipped
    and the scan continues.  When not even the strongest fragment fits, it is
    bisected so the answer-bearing head of it still arrives.
    """

    kept: list[EvidenceFragment] = []
    for fragment in fragments:
        if _fits((*kept, fragment), max_bytes):
            kept.append(fragment)
    if kept:
        return tuple(kept)
    if not fragments:
        return ()
    head = fragments[0]
    low, high = 0, len(head.text)
    best: EvidenceFragment | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = replace(head, end=head.start + middle, text=head.text[:middle])
        if middle > 0 and _fits((candidate,), max_bytes):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return (best,) if best is not None else ()


def _address_of(entry: Any, scope_id: str) -> MemoryEntryAddress | None:
    """Return the exact entry address behind one prepared entry.

    The builder represents a hit as a bare ``MemoryCitation`` when it belongs to the
    current scope and as a ``MemoryEntryAddress`` when it crosses scopes; selection
    has to accept both, because it must keep whatever identity the caller will get
    back in ``origins``.
    """

    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return origin
    memory_ref = getattr(origin, "memory_ref", None)
    entry_id = getattr(origin, "entry_id", None)
    entry_version_id = getattr(origin, "entry_version_id", None)
    if memory_ref is None or entry_id is None or entry_version_id is None:
        return None
    return MemoryEntryAddress(
        memory=ArtifactAddress(scope_id=scope_id, artifact=memory_ref),
        entry_id=entry_id,
        entry_version_id=entry_version_id,
    )


@dataclass(frozen=True)
class _Span:
    """One candidate sentence with the lexical shape selection needs."""

    address: MemoryEntryAddress
    entry: int
    start: int
    end: int
    text: str
    counts: Counter[str]
    length: int
    # The full entry body this span was cut from. Joining two accepted sentences has
    # to reproduce everything between them verbatim, including sentences that were
    # considered and rejected, so the merge slices the original text.
    source: str = ""


def _span_key(span: _Span) -> tuple[str, str, int]:
    return (span.address.entry_id, span.address.entry_version_id, span.start)


def _merge(entries: Sequence[tuple[float, _Span]]) -> list[EvidenceFragment]:
    """Join adjacent sentences of one entry into a single verbatim fragment.

    Two neighbouring sentences cost two citations in the envelope; delivering them
    as one span costs one, and the bytes that saves go to evidence text instead.
    """

    grouped: dict[tuple[str, str], list[tuple[float, _Span]]] = {}
    for value, span in entries:
        grouped.setdefault((span.address.entry_id, span.address.entry_version_id), []).append((value, span))
    merged: list[tuple[float, EvidenceFragment]] = []
    for group in grouped.values():
        group.sort(key=lambda item: item[1].start)
        value, current = group[0]
        start, end = current.start, current.end
        for span_value, span in group[1:]:
            # Merge across the joining whitespace only: two sentences separated by a
            # paragraph break stay separate evidence, but a run inside one paragraph
            # becomes a single span and saves a citation.
            if span.start >= end and not current.source[end : span.start].strip():
                end = span.end
                value = max(value, span_value)
                continue
            merged.append(
                (
                    value,
                    EvidenceFragment(
                        address=current.address, start=start, end=end, text=current.source[start:end]
                    ),
                )
            )
            current, start, end, value = span, span.start, span.end, span_value
        merged.append(
            (value, EvidenceFragment(address=current.address, start=start, end=end, text=current.source[start:end]))
        )
    merged.sort(key=lambda item: (-item[0], item[1].start))
    return [fragment for _value, fragment in merged]


def select_evidence(
    entries: Sequence[Any],
    *,
    query: str,
    scope_id: str,
    max_bytes: int,
) -> EvidenceSelection:
    """Choose the query-bearing spans of ``entries`` that fit ``max_bytes``."""

    query_terms = tuple(dict.fromkeys(_terms(query)))
    if not entries or not query_terms:
        return EvidenceSelection(content=None, fragments=())

    spans: list[_Span] = []
    for rank, entry in enumerate(entries):
        address = _address_of(entry, scope_id)
        if address is None:
            continue
        text = entry.content or ""
        for start, end in _sentence_spans(text):
            counts = Counter(_terms(text[start:end]))
            if not counts:
                continue
            spans.append(
                _Span(
                    address=address,
                    entry=rank,
                    start=start,
                    end=end,
                    text=text[start:end],
                    counts=counts,
                    length=sum(counts.values()),
                    source=text,
                )
            )
    if not spans:
        return EvidenceSelection(content=None, fragments=())

    weights = _idf(query_terms, [span.counts for span in spans])
    # An entry whose whole body covers the query is a better source of evidence than
    # one that happens to share a single term, so each entry contributes a bounded
    # prior instead of competing sentence-to-sentence on equal terms.
    coverage_mass: dict[int, float] = {}
    for span in spans:
        coverage_mass[span.entry] = coverage_mass.get(span.entry, 0.0) + sum(
            weights[term] for term in span.counts.keys() & weights.keys()
        )
    best_mass = max(coverage_mass.values(), default=0.0) or 1.0
    prior = {entry: mass / best_mass for entry, mass in coverage_mass.items()}
    average_length = max(sum(span.length for span in spans) / len(spans), 1.0)

    value_of: dict[tuple[str, str, int], float] = {}
    pool: list[tuple[float, _Span]] = []
    for span in spans:
        value = _sentence_score(span.counts, span.length, weights, average_length, prior.get(span.entry, 0.0))
        value_of[_span_key(span)] = value
        if value > 0.0:
            pool.append((value, span))
    if not pool:
        return EvidenceSelection(content=None, fragments=())

    # Order ties by entry then position so the same inputs always yield the same body.
    pool.sort(key=lambda item: (-item[0], item[1].entry, item[1].start))
    wanted_terms = set(weights)

    # Phase 1: greedy coverage. A sentence that carries a query term nothing selected
    # so far carries is worth more than another sentence reusing a covered one, which
    # is what keeps every part of the query represented instead of only its head.
    ranked: list[_Span] = []
    remaining = list(pool)
    covered: set[str] = set()
    while remaining:
        best_index = 0
        best_value = None
        for index, (base, span) in enumerate(remaining):
            gain = sum(weights[term] for term in span.counts.keys() & wanted_terms if term not in covered)
            value = base + _COVERAGE_WEIGHT * gain
            if best_value is None or value > best_value:
                best_index, best_value = index, value
        _, span = remaining.pop(best_index)
        covered |= span.counts.keys() & wanted_terms
        ranked.append(span)

    # Phase 2 then 3: the neighbours of chosen evidence, then anything else worth
    # having. The byte budget is a hard cap and the metric counts evidence that
    # arrived, so leaving budget unused would be evidence given away for free.
    by_entry: dict[int, list[_Span]] = {}
    for span in spans:
        by_entry.setdefault(span.entry, []).append(span)
    for group in by_entry.values():
        group.sort(key=lambda span: span.start)

    accepted: list[tuple[float, _Span]] = []
    taken: set[tuple[str, str, int]] = set()

    def offer(span: _Span) -> bool:
        key = _span_key(span)
        if key in taken:
            return True
        trial = _merge([*accepted, (value_of.get(key, 0.0), span)])
        if len(trial) > _MAX_FRAGMENTS or not _fits(trial, max_bytes):
            return False
        accepted.append((value_of.get(key, 0.0), span))
        taken.add(key)
        return True

    for span in ranked:
        offer(span)

    for group in by_entry.values():
        for index, span in enumerate(group):
            if _span_key(span) not in taken:
                continue
            for neighbour in (index - 1, index + 1):
                if 0 <= neighbour < len(group):
                    offer(group[neighbour])

    fillers = sorted(
        (span for span in spans if _span_key(span) not in taken),
        key=lambda span: (-value_of.get(_span_key(span), 0.0), span.entry, span.start),
    )
    for span in fillers:
        offer(span)

    merged = _merge(accepted)
    kept = tuple(merged) if _fits(merged, max_bytes) else _pack_to_budget(merged, max_bytes)
    if not kept:
        return EvidenceSelection(content=None, fragments=())
    return EvidenceSelection(content=_render(kept), fragments=kept)

