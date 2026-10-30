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

_TOKEN = re.compile(r"[a-z0-9]+")
# A sentence is the unit of selection: keeping whole sentences makes the delivered
# evidence readable and lets one entry yield several disjoint spans.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_MAX_FRAGMENTS = 24


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


def _idf(query_terms: Sequence[str], corpus: Sequence[Sequence[str]]) -> dict[str, float]:
    """Weight query terms by how rare they are across the candidate entries.

    A term that every entry repeats ("the", "notes") cannot tell evidence apart,
    so weighting by rarity is what lets a single distinctive word in the tail of a
    long document win the budget.
    """

    total = max(1, len(corpus))
    weights: dict[str, float] = {}
    for term in query_terms:
        containing = sum(1 for document in corpus if term in document)
        weights[term] = math.log(1.0 + total / max(1, containing))
    return weights


def _score(terms: Sequence[str], weights: dict[str, float], length: int) -> float:
    """Sum the rarity weights of the query terms a span contains, length-normalized."""

    if not terms:
        return 0.0
    seen = set(terms)
    mass = sum(weight for term, weight in weights.items() if term in seen)
    # A long span that happens to repeat one common word should not outrank the
    # short sentence that carries the rare ones.
    return mass / (1.0 + math.log(1.0 + max(0, length)))


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


def _cut_to_budget(fragments: Sequence[EvidenceFragment], max_bytes: int) -> tuple[EvidenceFragment, ...]:
    """Drop fragments, then shorten the tail, until the envelope fits the budget.

    Later fragments score lower, so the tail is what gives way first; the leading
    fragment is bisected last so the strongest evidence survives even alone.
    """

    kept = list(fragments)
    while kept and not _fits(kept, max_bytes):
        if len(kept) == 1:
            break
        kept.pop()
    if not kept:
        return ()
    if _fits(kept, max_bytes):
        return tuple(kept)
    head, rest = kept[0], tuple(kept[1:])
    low, high = 0, len(head.text)
    best: EvidenceFragment | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = replace(head, end=head.start + middle, text=head.text[:middle])
        if middle > 0 and _fits((candidate, *rest), max_bytes):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return (best, *rest) if best is not None else rest


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


def select_evidence(
    entries: Sequence[Any],
    *,
    query: str,
    scope_id: str,
    max_bytes: int,
) -> EvidenceSelection:
    """Choose the query-bearing spans of ``entries`` that fit ``max_bytes``."""

    query_terms = _terms(query)
    if not entries or not query_terms:
        return EvidenceSelection(content=None, fragments=())

    corpus = [_terms(entry.content) for entry in entries]
    weights = _idf(query_terms, corpus)

    scored: list[tuple[float, int, MemoryEntryAddress, int, int, str]] = []
    for rank, entry in enumerate(entries):
        address = _address_of(entry, scope_id)
        if address is None:
            continue
        text = entry.content
        for start, end in _sentence_spans(text):
            span_terms = _terms(text[start:end])
            score = _score(span_terms, weights, end - start)
            if score <= 0.0:
                continue
            # Entry rank breaks ties so an earlier candidate keeps the position it
            # earned in recall, which keeps the choice stable across calls.
            scored.append((score, -rank, address, start, end, text[start:end]))
    if not scored:
        return EvidenceSelection(content=None, fragments=())

    scored.sort(key=lambda item: (-item[0], -item[1], item[3]))
    ordered: list[EvidenceFragment] = []
    seen: set[tuple[str, str, int, int]] = set()
    for score, _, address, start, end, text in scored:
        del score
        key = (address.entry_id, address.entry_version_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(EvidenceFragment(address=address, start=start, end=end, text=text))
        if len(ordered) >= _MAX_FRAGMENTS:
            break

    kept = _cut_to_budget(ordered, max_bytes)
    if not kept:
        return EvidenceSelection(content=None, fragments=())
    return EvidenceSelection(content=_render(kept), fragments=kept)

