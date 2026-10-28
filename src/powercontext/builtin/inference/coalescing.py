"""Coalescing embedding model wrapper.

Deduplicates concurrent embedding requests, enforces bounded batch sizes,
and caches recent results with LRU eviction.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass
class _PendingBatch:
    """One shared coalesce batch collecting texts from concurrent callers."""

    texts: list[str] = field(default_factory=list)
    task_started: bool = False
    caller_texts: dict[int, list[str]] = field(default_factory=dict)
    caller_indices: dict[int, list[int]] = field(default_factory=dict)
    caller_results: dict[int, dict[str, tuple[float, ...]]] = field(default_factory=dict)
    caller_events: dict[int, asyncio.Event] = field(default_factory=dict)
    caller_usage: dict[int, int] = field(default_factory=dict)
    provider_usage: InferenceUsage | None = None
    error: BaseException | None = None


class CoalescingEmbeddingModel:
    """Wrap an embedding model with dedup, batching, and LRU caching."""

    def __init__(
        self,
        base: EmbeddingModel,
        *,
        max_batch_size: int = 100,
        coalesce_ms: int = 0,
        cache_size: int = 1000,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        self._base = base
        self._max_batch_size = max_batch_size
        self._coalesce_ms = coalesce_ms
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._lock = threading.Lock()
        self._pending: _PendingBatch | None = None

    @property
    def profile(self) -> Any:
        return self._base.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        vectors: list[tuple[float, ...]] = []
        texts_needing_embed: list[str] = []
        indices_needing_embed: list[int] = []

        for i, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                vectors.append(cached)
            else:
                vectors.append(())
                texts_needing_embed.append(text)
                indices_needing_embed.append(i)

        if not texts_needing_embed:
            return EmbeddingResult(
                vectors=tuple(vectors),
                usage=InferenceUsage(requests=0, input_tokens=0, output_tokens=0),
            )

        caller_id = id(texts)
        event = asyncio.Event()
        start_task = False

        with self._lock:
            if self._pending is None:
                self._pending = _PendingBatch()
            batch = self._pending
            batch.caller_texts[caller_id] = list(texts_needing_embed)
            batch.caller_indices[caller_id] = list(indices_needing_embed)
            batch.caller_results[caller_id] = {}
            batch.caller_events[caller_id] = event
            batch.caller_usage[caller_id] = 0
            batch.texts.extend(texts_needing_embed)
            if not batch.task_started:
                batch.task_started = True
                start_task = True

        if start_task:
            loop = asyncio.get_running_loop()
            if self._coalesce_ms > 0:
                loop.create_task(self._delayed_process(batch))
            else:
                loop.create_task(self._process_batch(batch))

        await event.wait()

        if batch.error is not None:
            raise batch.error

        results = batch.caller_results[caller_id]
        for j, idx in enumerate(indices_needing_embed):
            vectors[idx] = results[texts_needing_embed[j]]

        usage_requests = batch.caller_usage[caller_id]
        provider = batch.provider_usage or InferenceUsage(requests=0)

        if usage_requests == 0:
            usage = InferenceUsage(requests=0, input_tokens=0, output_tokens=0)
        else:
            usage = InferenceUsage(
                requests=usage_requests,
                input_tokens=provider.input_tokens,
                output_tokens=provider.output_tokens,
            )

        return EmbeddingResult(vectors=tuple(vectors), usage=usage)

    async def _delayed_process(self, batch: _PendingBatch) -> None:
        await asyncio.sleep(self._coalesce_ms / 1000.0)
        await self._process_batch(batch)

    async def _process_batch(self, batch: _PendingBatch) -> None:
        last_result: EmbeddingResult | None = None
        try:
            unique_texts = list(dict.fromkeys(batch.texts))
            text_to_batch_index: dict[str, int] = {}
            total_requests = 0

            for start in range(0, len(unique_texts), self._max_batch_size):
                chunk = tuple(unique_texts[start : start + self._max_batch_size])
                batch_index = start // self._max_batch_size
                last_result = await asyncio.shield(self._base.embed(chunk))
                if len(last_result.vectors) != len(chunk):
                    raise ValueError(
                        f"provider returned {len(last_result.vectors)} vectors for {len(chunk)} texts"
                    )
                for j, text in enumerate(chunk):
                    self._cache_put(text, last_result.vectors[j])
                    text_to_batch_index[text] = batch_index
                total_requests += 1

            for caller_id, caller_text_list in batch.caller_texts.items():
                batch_indices = set()
                for text in caller_text_list:
                    if text in text_to_batch_index:
                        batch_indices.add(text_to_batch_index[text])
                batch.caller_usage[caller_id] = len(batch_indices)

            for caller_id, caller_text_list in batch.caller_texts.items():
                for text in caller_text_list:
                    cached = self._cache_get(text)
                    if cached is not None:
                        batch.caller_results[caller_id][text] = cached

            if total_requests > 0 and last_result is not None:
                provider_usage = last_result.usage
                batch.provider_usage = InferenceUsage(
                    requests=total_requests,
                    input_tokens=provider_usage.input_tokens,
                    output_tokens=provider_usage.output_tokens,
                )
            else:
                batch.provider_usage = InferenceUsage(requests=0)
        except BaseException as e:
            batch.error = e
        finally:
            with self._lock:
                if self._pending is batch:
                    self._pending = None
            for evt in batch.caller_events.values():
                evt.set()

    def _cache_get(self, text: str) -> tuple[float, ...] | None:
        try:
            value = self._cache[text]
            self._cache.move_to_end(text)
            return value
        except KeyError:
            return None

    def _cache_put(self, text: str, vector: tuple[float, ...]) -> None:
        if text in self._cache:
            self._cache.move_to_end(text)
            self._cache[text] = vector
        else:
            self._cache[text] = vector
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)


__all__ = ["CoalescingEmbeddingModel"]
