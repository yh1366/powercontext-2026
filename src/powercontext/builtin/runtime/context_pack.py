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


@dataclass(frozen=True)
class ContextPack:
    """An immutable, content-addressed pack of byte items."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> Self:
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
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError) as error:
            raise ContextPackError("invalid JSON") from error

        for field in ("schema", "items", "digest", "byte_count"):
            if field not in payload:
                raise ContextPackError(f"missing field: {field}")

        items_encoded = payload["items"]
        if not isinstance(items_encoded, list):
            raise ContextPackError("items must be an array")

        try:
            items = tuple(base64.b64decode(item) for item in items_encoded)
        except Exception as error:
            raise ContextPackError("invalid base64 item") from error

        pack = cls.build(items)
        if pack.digest != payload["digest"]:
            raise ContextPackError("digest mismatch")
        if pack.byte_count != payload["byte_count"]:
            raise ContextPackError("byte_count mismatch")
        return pack

    @classmethod
    def from_content(cls, text: str, *, chunk_bytes: int = 4096) -> Self:
        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")
        encoded = text.encode("utf-8")
        items: list[bytes] = []
        pos = 0
        while pos < len(encoded):
            end = min(pos + chunk_bytes, len(encoded))
            if end < len(encoded):
                while end > pos and (encoded[end] & 0xC0) == 0x80:
                    end -= 1
                if end == pos:
                    raise ContextPackError(
                        f"character at byte {pos} does not fit in chunk_bytes={chunk_bytes}"
                    )
            items.append(encoded[pos:end])
            pos = end
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
            "schema": "powercontext.context-pack-delta.v1",
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": encoded,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError) as error:
            raise ContextPackError("invalid JSON") from error

        for field in ("schema", "base_digest", "target_digest", "prefix_items", "appended"):
            if field not in payload:
                raise ContextPackError(f"missing field: {field}")

        if payload["schema"] != "powercontext.context-pack-delta.v1":
            raise ContextPackError("wrong schema")

        prefix = payload["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")

        appended_encoded = payload["appended"]
        if not isinstance(appended_encoded, list):
            raise ContextPackError("appended must be an array")

        try:
            appended = tuple(base64.b64decode(item) for item in appended_encoded)
        except Exception as error:
            raise ContextPackError("invalid base64 appended item") from error

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
