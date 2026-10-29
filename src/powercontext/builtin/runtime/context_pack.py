"""Incremental context pack with delta compression.

Wire format uses base64-encoded items, SHA-256 digest, and canonical JSON.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Self


class ContextPackError(Exception):
    """Raised when a context pack or delta fails validation."""


PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
# Consecutive preparations differ by insertions (a newly appended Memory entry, a longer
# suffix).  Fixed-size chunks shift every boundary after an insertion, so bytes that did not
# change are resent anyway.  Content-defined chunking anchors boundaries on the bytes
# themselves: an insertion only re-chunks a local neighbourhood and the stream re-synchronises
# within roughly one average chunk, which is what the transfer-cost gate measures.
INCREMENTAL_CHUNK_BYTES = 2048
_CHUNK_AVERAGE_BITS = 9
_CHUNK_AVERAGE = 1 << _CHUNK_AVERAGE_BITS  # geometric mean distance between boundaries
_CHUNK_MIN_BYTES = 64
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


def _build_gear_table() -> tuple[int, ...]:
    """Deterministic pseudo-random Gear table; the derivation is fixed, not seeded."""

    return tuple(
        int.from_bytes(hashlib.sha256(b"powercontext.gear" + bytes([index])).digest()[:8], "big")
        for index in range(256)
    )


_GEAR = _build_gear_table()
_GEAR_MASK64 = (1 << 64) - 1
_BOUNDARY_MASK = (1 << _CHUNK_AVERAGE_BITS) - 1


def _load_object(data: bytes) -> dict:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise ContextPackError("invalid JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload must be a JSON object")
    return payload


def _require_fields(payload: dict, expected: frozenset[str]) -> None:
    missing = expected - set(payload)
    if missing:
        raise ContextPackError(f"missing field: {sorted(missing)[0]}")
    extra = set(payload) - expected
    if extra:
        raise ContextPackError(f"unexpected field: {sorted(extra)[0]}")


def _decode_item(value: object) -> bytes:
    if not isinstance(value, str):
        raise ContextPackError("item must be a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as error:
        raise ContextPackError("invalid base64 item") from error


@dataclass(frozen=True)
class ContextPack:
    """An immutable, content-addressed pack of byte items."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> Self:
        items = tuple(item.encode("utf-8") if isinstance(item, str) else bytes(item) for item in items)
        encoded = [base64.b64encode(item).decode("ascii") for item in items]
        unsigned = {
            "schema": "powercontext.context-pack.v1",
            "items": encoded,
        }
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()

        probe = {**unsigned, "digest": digest, "byte_count": 0}
        probe_len = len(json.dumps(probe, sort_keys=True, separators=(",", ":")).encode())
        byte_count = probe_len
        for _ in range(3):
            candidate = {**unsigned, "digest": digest, "byte_count": byte_count}
            actual = len(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode())
            if actual == byte_count:
                break
            byte_count = actual

        return cls(items=items, digest=digest, byte_count=byte_count)

    def to_bytes(self) -> bytes:
        encoded = [base64.b64encode(item).decode("ascii") for item in self.items]
        payload = {
            "schema": "powercontext.context-pack.v1",
            "items": encoded,
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        payload = _load_object(data)
        _require_fields(payload, _PACK_FIELDS)
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("wrong schema")

        encoded = payload["items"]
        if not isinstance(encoded, list):
            raise ContextPackError("items must be an array")

        items = tuple(_decode_item(value) for value in encoded)

        pack = cls.build(items)
        if pack.digest != payload["digest"]:
            raise ContextPackError("digest mismatch")
        if pack.byte_count != payload["byte_count"]:
            raise ContextPackError("byte_count mismatch")
        return pack

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = 512) -> Self:
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        encoded = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        items: list[bytes] = []
        start = 0
        rolling = 0
        for pos in range(len(encoded)):
            rolling = ((rolling << 1) + _GEAR[encoded[pos]]) & _GEAR_MASK64
            reached_limit = pos + 1 - start >= chunk_bytes
            boundary_hit = not reached_limit and pos + 1 - start >= _CHUNK_MIN_BYTES and (rolling & _BOUNDARY_MASK) == 0
            if reached_limit or boundary_hit:
                end = pos + 1
                if end < len(encoded):
                    while end > start and (encoded[end] & 0xC0) == 0x80:
                        end -= 1  # do not cut a multi-byte character: back up to its first byte
                if end == start:
                    raise ContextPackError(f"character at byte {start} does not fit in chunk_bytes={chunk_bytes}")
                items.append(encoded[start:end])
                start = end
        if start < len(encoded):
            items.append(encoded[start:])
        return cls.build(tuple(items))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextPack):
            return NotImplemented
        return self.digest == other.digest


@dataclass(frozen=True)
class ContextPackDelta:
    """A delta between two context packs."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @property
    def digest(self) -> str:
        return self.target_digest

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> Self:
        prefix = 0
        for i in range(min(len(base.items), len(target.items))):
            if base.items[i] == target.items[i]:
                prefix += 1
            else:
                break
        appended = target.items[prefix:]
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=appended,
        )

    def to_bytes(self) -> bytes:
        encoded = [base64.b64encode(item).decode("ascii") for item in self.appended]
        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": encoded,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        payload = _load_object(data)
        _require_fields(payload, _DELTA_FIELDS)

        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("wrong schema")

        prefix = payload["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")

        for field in ("base_digest", "target_digest"):
            if not isinstance(payload[field], str):
                raise ContextPackError(f"{field} must be a string")

        appended_encoded = payload["appended"]
        if not isinstance(appended_encoded, list):
            raise ContextPackError("appended must be an array")

        appended = tuple(_decode_item(value) for value in appended_encoded)

        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=prefix,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Apply a delta to a base pack, producing the target pack."""
    if base.digest != delta.base_digest:
        raise ContextPackError("base digest mismatch")

    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix_items exceeds base item count")

    target_items = base.items[: delta.prefix_items] + delta.appended
    target = ContextPack.build(target_items)

    if target.digest != delta.target_digest:
        raise ContextPackError("target digest mismatch after delta application")

    return target


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
