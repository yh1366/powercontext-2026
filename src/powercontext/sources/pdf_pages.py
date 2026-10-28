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

"""Page-level PDF Sources and evidence-page retrieval over scoped Memory.

Every physical page of one original PDF becomes its own ``CAPTURED`` Source
identified by the document, the whole-file digest and the 1-based physical page
number, so a retrieval citation always resolves to the exact page of the exact
revision it was extracted from.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import math
import re
import weakref
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_TYPE = "pdf-page"
PDF_PAGE_DEFINITION_VERSION = "1"
SOURCE_ID_PREFIX = "pdf"
SOURCE_ID_SEPARATOR = ":"

_TEXT_TOKEN = re.compile(r"[a-z0-9]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]+")
_LINE_END_HYPHEN = re.compile(r"(?<=[A-Za-z])-[ \t]*\n[ \t]*(?=[a-z])")
_BLANK_RUN = re.compile(r"[ \t]+\n")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HEADING_PREFIX = re.compile(r"^(item|note|part|section|schedule)\s+[0-9ivx]", re.IGNORECASE)

_QUERY_FUNCTION_WORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "can",
    "could",
    "should",
    "would",
    "will",
    "shall",
    "may",
    "might",
    "must",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "their",
    "this",
    "that",
    "these",
    "those",
    "there",
    "also",
    "any",
    "all",
    "if",
    "than",
    "then",
    "so",
    "such",
    "per",
    "please",
    "give",
    "respond",
    "response",
    "rely",
    "relying",
    "provided",
    "supplied",
    "question",
    "answer",
])

# Retrieval tuning. These are generic information-retrieval constants; none of
# them is derived from a particular document, question or answer.
_BM25_K1 = 1.2
_BM25_B = 0.45
_HEADING_WEIGHT = 2.4
_PROXIMITY_WEIGHT = 0.6
_FUSION_WEIGHT = 25.0
_FUSION_SCALE = 60.0
_DOCUMENT_BOOST = 0.35
_CANDIDATE_PER_QUERY = 50
_CORPUS_LIMIT = 4000
_CORPUS_PROBES = ("the", "of", "and", "to", "in", "for")
_MIN_COLLECTION = 32
_MAX_TERMS = 14
_MAX_TERM_QUERIES = 11
_PROXIMITY_WINDOW = 40
_PROXIMITY_STRIDE = 20
_KEY_LENGTH = 6

_COLLECTION_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document addressed by document identity."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a PDF revision, in extraction order."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A single captured PDF page with a revision-aware stable identity."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    definition_version: str = PDF_PAGE_DEFINITION_VERSION
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the revision-stable identity of one physical page."""

    _require_document_id(document_id)
    _require_digest(document_sha256)
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("pdf page_number must be a positive integer")  # noqa: TRY003
    quoted = quote(document_id, safe="")
    identity = f"{SOURCE_ID_PREFIX}{SOURCE_ID_SEPARATOR}{quoted}{SOURCE_ID_SEPARATOR}{document_sha256}"
    identity = f"{identity}{SOURCE_ID_SEPARATOR}{page_number}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        # Keep long document identifiers stable and collision-free without exceeding the cap.
        abbreviated = hashlib.sha256(quoted.encode("utf-8")).hexdigest()[:60]
        identity = (
            f"{SOURCE_ID_PREFIX}{SOURCE_ID_SEPARATOR}{abbreviated}"
            f"{SOURCE_ID_SEPARATOR}{document_sha256}{SOURCE_ID_SEPARATOR}{page_number}"
        )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pdf page source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page of one PDF revision atomically."""

    document_id = value.document_id
    _require_document_id(document_id)
    content = value.content
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("pdf content must be bytes")  # noqa: TRY003, TRY004
    raw = bytes(content)
    if not raw:
        raise ValueError("pdf content must not be empty")  # noqa: TRY003

    digest = hashlib.sha256(raw).hexdigest()
    reader = _open_reader(raw)
    total = _page_count(reader)

    pages: list[PDFPageInput] = []
    for index in range(total):
        number = index + 1
        text = _page_text(reader, index)
        page = PDFPageInput(
            document_id=document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        # Fail before returning any page when an identity cannot be built.
        pdf_page_source_id(document_id, digest, number)
        pages.append(page)
    return tuple(pages)


async def search_pdf_pages(
    memory,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct page citations for one natural-language question.

    Retrieval is two-stage and entirely offline. The scoped Memory is first read
    through broad lexical probes, which yields the page collection the question
    is answered from; the question's own terms then rank that collection with
    corpus-exact inverse document frequency, heading agreement and term
    proximity, and finally fuse the lexical search rank of the whole question.
    Every returned value is a real Memory citation the caller resolves back to
    the exact page.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return ()
    terms = _question_terms(question)
    if not terms:
        return ()

    from powercontext.builtin.runtime import SearchMemoryRequest

    pages = await _pages(memory, terms, SearchMemoryRequest)
    if not pages:
        return ()
    ranking = await _search(
        memory,
        SearchMemoryRequest,
        " ".join(term.text for term in terms[:_MAX_TERMS]),
        _CORPUS_LIMIT,
    )
    fused = {hit.entry_id: rank for rank, hit in enumerate(ranking.hits)}
    ranked = _rank(pages, terms, fused)
    return tuple(page.citation for page in ranked[:limit])


@dataclass(frozen=True)
class _Term:
    """One question term with the surface forms worth probing."""

    text: str
    key: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class _Page:
    """One page of the searched collection with its lexical profile."""

    citation: MemoryCitation
    profile: _Profile
    document_keys: frozenset[str]


@dataclass
class _Profile:
    """Lexical shape of one page."""

    counts: Counter[str] = field(default_factory=Counter)
    sequence: list[str] = field(default_factory=list)
    heading_keys: set[str] = field(default_factory=set)
    length: int = 0


class PDFPageSourceAdapter:
    """Resolve and read the page-level PDF Source described by one Definition."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        _require_page(value)
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one physical page as canonical standard text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_TYPE,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


def _open_reader(raw: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
    except Exception as error:
        raise ValueError(f"pdf document cannot be opened: {error}") from error  # noqa: TRY003
    if reader.is_encrypted:
        _unlock(reader)
    return reader


def _unlock(reader: PdfReader) -> None:
    try:
        status = reader.decrypt("")
    except Exception as error:
        message = "pdf document requires a password that was not supplied"
        raise ValueError(message) from error
    if not status:
        message = "pdf document requires a password that was not supplied"
        raise ValueError(message)


def _page_count(reader: PdfReader) -> int:
    try:
        total = len(reader.pages)
    except Exception as error:
        raise ValueError(f"pdf document pages cannot be read: {error}") from error  # noqa: TRY003
    if total < 1:
        raise ValueError("pdf document must contain at least one physical page")  # noqa: TRY003
    return total


def _page_text(reader: PdfReader, index: int) -> str:
    try:
        page = reader.pages[index]
        extracted = page.extract_text(extraction_mode="plain")
    except Exception as error:
        message = f"pdf page {index + 1} cannot be extracted"
        raise ValueError(message) from error
    return _clean_text(extracted or "")


def _clean_text(value: str) -> str:
    """Normalize extracted page text without changing its word sequence."""

    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    text = _LINE_END_HYPHEN.sub("", text)
    text = _BLANK_RUN.sub("\n", text)
    return text.strip()


def _citation_of(hit) -> MemoryCitation:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    return MemoryCitation(
        memory_ref=hit.memory_ref,
        entry_id=hit.entry_id,
        entry_version_id=hit.entry_version_id,
    )


def _question_terms(question: str) -> tuple[_Term, ...]:
    """Reduce one question to its ordered, non-functional evidence terms."""

    if not isinstance(question, str) or not question.strip():
        return ()
    tokens = _TEXT_TOKEN.findall(question.casefold())
    terms: list[_Term] = []
    seen: set[str] = set()
    for token in tokens:
        if token in _QUERY_FUNCTION_WORDS or token in seen:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        seen.add(token)
        terms.append(_Term(text=token, key=_key(token), variants=_surface_variants(token)))
        if len(terms) == _MAX_TERMS:
            break
    return tuple(terms)


def _term_queries(term: _Term) -> tuple[str, ...]:
    """Return the narrow probes for one term.

    Each probe carries at most two tokens, so a candidate only has to share one
    real Analyzer term to survive the shared admission floor.
    """

    variants = term.variants or (term.text,)
    queries = [" ".join(variants[:2])]
    if len(variants) > 2:
        alternate = " ".join((variants[0], variants[2]))
        if alternate not in queries:
            queries.append(alternate)
    return tuple(queries)


def _surface_variants(token: str) -> tuple[str, ...]:  # noqa: C901
    """Return plausible surface forms the analyzer may have stored for one term.

    Retrieval is exact-token, so one morphological family has to be probed
    explicitly; the variants are generic English inflection and derivation
    rules, not document-specific knowledge.
    """

    variants: list[str] = [token]

    def add(value: str) -> None:
        if value and value not in variants and len(variants) < 5:
            variants.append(value)

    numeric = re.match(r"^([a-z]+)(\d+)$", token)
    if numeric is not None:
        add(numeric.group(1))
        add(numeric.group(2))
        return tuple(variants)
    reversed_numeric = re.match(r"^(\d+)([a-z]+)$", token)
    if reversed_numeric is not None:
        add(reversed_numeric.group(1))
        return tuple(variants)
    if token.isdigit():
        return tuple(variants)

    if token.endswith("ies") and len(token) > 4:
        add(token[:-3] + "y")
    elif token.endswith("es") and len(token) > 4:
        add(token[:-2])
        add(token[:-1])
    elif token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        add(token[:-1])
    if token.endswith("ing") and len(token) > 5:
        add(token[:-3])
        add(token[:-3] + "e")
    if token.endswith("ed") and len(token) > 4:
        add(token[:-2])
        add(token[:-2] + "e")
    if token.endswith("ly") and len(token) > 4:
        add(token[:-2])
    if token.endswith("ment") and len(token) > 5:
        add(token[:-4])
    if token.endswith("ness") and len(token) > 5:
        add(token[:-4])
    if token.endswith("ation") and len(token) > 6:
        add(token[:-5] + "e")
        add(token[:-5] + "ate")
    if token.endswith("tion") and len(token) > 5:
        add(token[:-4] + "t")
        add(token[:-4] + "te")
    if token.endswith("ally") and len(token) > 5:
        add(token[:-4])
        add(token[:-4] + "al")
    if token.endswith("ic") and len(token) > 5:
        add(token[:-2] + "y")
        add(token + "al")
    if token.endswith("y") and len(token) > 6:
        add(token[:-1] + "ic")
        add(token[:-1] + "ical")
    return tuple(variants)


def _key(token: str) -> str:
    """Comparison key: a light stem truncated to a shared prefix."""

    if token.isdigit():
        return token
    stem = _stem(token)
    return stem[:_KEY_LENGTH] if len(stem) > _KEY_LENGTH else stem


def _stem(token: str) -> str:
    """Reduce one token to a comparison stem shared by pages and questions."""

    if token.isdigit() or len(token) < 5:
        return token
    if token.endswith(("ies", "ied")):
        return token[:-3] + "y"
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ness", "ally"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    for suffix in ("ed", "es", "ly", "al", "er"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


async def _search(memory, request_class, query: str, limit: int):
    """Search one scoped Memory, tolerating a tighter backend page cap."""

    try:
        return await memory.search(request_class(query=query, limit=limit, mode="fts"))
    except Exception:
        return await memory.search(request_class(query=query, limit=min(limit, _CANDIDATE_PER_QUERY), mode="fts"))


async def _pages(memory, terms: tuple[_Term, ...], request_class) -> tuple[_Page, ...]:
    """Read the page collection a question is answered from.

    Broad lexical probes enumerate the collection once per scoped Memory and the
    result is reused while a probe still reports the same collection size. When a
    deployment caps result pages, the question's own narrow probes widen the
    candidate set instead.
    """

    try:
        cached = _COLLECTION_CACHE.get(memory)
    except TypeError:
        cached = None
    if cached is not None:
        pages, signature = cached
        result = await _search(memory, request_class, _CORPUS_PROBES[0], _CORPUS_LIMIT)
        if len(result.hits) == signature:
            return pages

    observed: dict[str, object] = {}
    signature = 0
    for index, probe in enumerate(_CORPUS_PROBES):
        result = await _search(memory, request_class, probe, _CORPUS_LIMIT)
        if index == 0:
            signature = len(result.hits)
        for hit in result.hits:
            observed.setdefault(hit.entry_id, hit)
    if len(observed) < _MIN_COLLECTION:
        for term in terms[:_MAX_TERM_QUERIES]:
            for query in _term_queries(term):
                result = await _search(memory, request_class, query, _CANDIDATE_PER_QUERY)
                for hit in result.hits:
                    observed.setdefault(hit.entry_id, hit)

    pages = tuple(
        _Page(
            citation=_citation_of(hit),
            profile=_profile(hit.text),
            document_keys=frozenset(_key(token) for token in _TEXT_TOKEN.findall(_document_of(hit.text))),
        )
        for hit in observed.values()
    )
    with contextlib.suppress(TypeError):
        _COLLECTION_CACHE[memory] = (pages, signature)
    return pages


def _document_of(text: str) -> str:
    """Return the document identity a page text was projected from."""

    head, _separator, _rest = text.partition("\n")
    return head.casefold()


def _rank(  # noqa: C901
    pages: tuple[_Page, ...], terms: tuple[_Term, ...], fused: dict[str, int]
) -> list[_Page]:
    """Rank the page collection against one question's lexical profile."""

    weights: dict[str, float] = {}
    document_frequency: Counter[str] = Counter()
    for page in pages:
        document_frequency.update(page.profile.counts.keys())
    total = max(len(pages), 1)
    for term in terms:
        frequency = document_frequency.get(term.key, 0)
        weight = math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        weights[term.key] = max(weights.get(term.key, 0.0), weight)
    total_weight = sum(weights.values()) or 1.0
    average_length = max(sum(page.profile.length for page in pages) / total, 1.0)

    document_affinity: dict[frozenset[str], float] = {}
    for page in pages:
        if page.document_keys in document_affinity:
            continue
        document_affinity[page.document_keys] = sum(weights[key] for key in page.document_keys.intersection(weights))
    best_document = max(document_affinity.values(), default=0.0)

    scored: list[tuple[float, int, _Page]] = []
    for order, page in enumerate(pages):
        profile = page.profile
        body = 0.0
        heading = 0.0
        for key, weight in weights.items():
            frequency = profile.counts.get(key, 0)
            if not frequency:
                continue
            saturation = (
                frequency
                * (_BM25_K1 + 1.0)
                / (frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * profile.length / average_length))
            )
            body += weight * saturation
            if key in profile.heading_keys:
                heading += weight
        proximity = _proximity(profile, weights) / total_weight
        score = body + _HEADING_WEIGHT * heading + _PROXIMITY_WEIGHT * proximity * total_weight
        rank = fused.get(page.citation.entry_id)
        if rank is not None:
            score += _FUSION_WEIGHT * _FUSION_SCALE / (_FUSION_SCALE + rank)
        if best_document > 0.0:
            score *= 1.0 + _DOCUMENT_BOOST * (document_affinity[page.document_keys] / best_document)
        scored.append((score, order, page))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [page for _score, _order, page in scored]


def _profile(text: str) -> _Profile:
    tokens = _TEXT_TOKEN.findall(text.casefold())
    counts: Counter[str] = Counter()
    sequence: list[str] = []
    for token in tokens:
        key = _key(token)
        counts[key] += 1
        sequence.append(key)
    return _Profile(counts=counts, sequence=sequence, heading_keys=_heading_keys(text), length=len(tokens))


def _heading_keys(text: str) -> set[str]:
    """Collect the lexical keys of heading-like lines on one page."""

    keys: set[str] = set()
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 110:
            continue
        tokens = _TEXT_TOKEN.findall(stripped.casefold())
        if not tokens or len(tokens) > 12:
            continue
        words = [word for word in _WORD.findall(stripped) if len(word) > 2]
        capitalised = sum(1 for word in words if word[0].isupper())
        is_heading = bool(_HEADING_PREFIX.match(stripped)) or (bool(words) and capitalised >= 0.7 * len(words))
        if is_heading:
            keys.update(_key(token) for token in tokens)
    return keys


def _proximity(profile: _Profile, weights: dict[str, float]) -> float:
    """Weighted coverage of the tightest window of one page."""

    sequence = profile.sequence
    if not sequence:
        return 0.0
    best = 0.0
    for start in range(0, len(sequence), _PROXIMITY_STRIDE):
        window = sequence[start : start + _PROXIMITY_WINDOW]
        present = {key for key in window if key in weights}
        if not present:
            continue
        best = max(best, sum(weights[key] for key in present))
    return best


def _require_document_id(document_id: object) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("pdf document_id must be a non-blank string")  # noqa: TRY003


def _require_digest(document_sha256: object) -> None:
    if not isinstance(document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", document_sha256):
        raise ValueError("pdf document_sha256 must be a lowercase hex SHA-256 digest")  # noqa: TRY003


def _require_page(value: PDFPageInput) -> None:
    _require_document_id(value.document_id)
    _require_digest(value.document_sha256)
    if not isinstance(value.page_number, int) or isinstance(value.page_number, bool) or value.page_number < 1:
        raise ValueError("pdf page_number must be a positive integer")  # noqa: TRY003
    if not isinstance(value.text, str):
        raise ValueError("pdf page text must be a string")  # noqa: TRY003, TRY004


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_DEFINITION_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


__all__ = [
    "PDF_PAGE_DEFINITION_VERSION",
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_TYPE",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
