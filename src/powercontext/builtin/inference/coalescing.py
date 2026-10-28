"""Coalescing embedding model wrapper.

Shares in-flight work between concurrent callers, batches every provider
request to a bounded size, and keeps a bounded LRU of successful vectors.

The unit of sharing is a *text*: every distinct text owns at most one
in-flight future, so a caller that arrives while a request is still running
joins that request instead of issuing a duplicate one.  A cancelled caller
only drops its own ``await`` (the shared future is shielded), and a failing
batch only fails the callers that depend on it.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


class _BatchRecord:
    """The provider request a text was resolved by."""

    __slots__ = ("batch_id",)

    def __init__(self, batch_id: int) -> None:
        self.batch_id = batch_id


class CoalescingEmbeddingModel:
    """Wrap an embedding model with dedup, batching, and LRU caching."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)):
            raise ValueError("coalesce_ms must be a number")
        if not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")

        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size

        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._pending: list[str] = []
        self._batch_of_text: dict[str, _BatchRecord] = {}
        self._batch_usage: dict[int, InferenceUsage] = {}
        self._next_batch_id = 0
        self._dispatcher: asyncio.Task | None = None

    @property
    def profile(self) -> Any:
        return self._model.profile

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        wanted = tuple(texts)
        loop = asyncio.get_running_loop()
        resolved: dict[str, tuple[float, ...]] = {}
        waits: dict[str, asyncio.Future] = {}

        for text in wanted:
            if text in resolved or text in waits:
                continue
            hit = self._cache_get(text)
            if hit is not None:
                resolved[text] = hit
                continue
            future = self._inflight.get(text)
            if future is None:
                future = loop.create_future()
                self._inflight[text] = future
                self._pending.append(text)
                self._ensure_dispatcher()
            waits[text] = future

        if waits:
            outcomes = await asyncio.gather(*(asyncio.shield(future) for future in waits.values()))
            for text, vector in zip(waits, outcomes):
                resolved[text] = tuple(vector)

        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in wanted),
            usage=self._usage_for(tuple(waits)),
        )

    def _usage_for(self, texts: Sequence[str]) -> InferenceUsage:
        distinct = sorted(
            {record.batch_id for text in texts if (record := self._batch_of_text.get(text)) is not None}
        )
        if not distinct:
            return InferenceUsage(requests=0, input_tokens=0, output_tokens=0)
        input_tokens: int | None = 0
        output_tokens: int | None = 0
        for batch_id in distinct:
            usage = self._batch_usage.get(batch_id)
            if usage is None:
                continue
            if usage.input_tokens is None or input_tokens is None:
                input_tokens = None
            else:
                input_tokens += usage.input_tokens
            if usage.output_tokens is None or output_tokens is None:
                output_tokens = None
            else:
                output_tokens += usage.output_tokens
        return InferenceUsage(requests=len(distinct), input_tokens=input_tokens, output_tokens=output_tokens)

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is not None and not self._dispatcher.done():
            return
        self._dispatcher = asyncio.get_running_loop().create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        while self._pending:
            if self._coalesce_ms > 0:
                await asyncio.sleep(self._coalesce_ms / 1000.0)
            texts = list(self._pending)
            self._pending.clear()
            for start in range(0, len(texts), self._max_batch_size):
                await self._run_batch(texts[start : start + self._max_batch_size])

    async def _run_batch(self, chunk: Sequence[str]) -> None:
        batch_id = self._next_batch_id
        self._next_batch_id += 1
        record = _BatchRecord(batch_id)
        for text in chunk:
            self._batch_of_text[text] = record
        try:
            result = await asyncio.shield(self._model.embed(tuple(chunk)))
            vectors = tuple(tuple(vector) for vector in result.vectors)
            if len(vectors) != len(chunk):
                raise ValueError(f"provider returned {len(vectors)} vectors for {len(chunk)} texts")
        except BaseException as error:
            for text in chunk:
                future = self._inflight.pop(text, None)
                if future is not None and not future.done():
                    future.set_exception(error)
            if isinstance(error, asyncio.CancelledError):
                raise
            return

        self._batch_usage[batch_id] = result.usage or InferenceUsage(requests=1)
        for text, vector in zip(chunk, vectors):
            self._cache_put(text, vector)
        for text, vector in zip(chunk, vectors):
            future = self._inflight.pop(text, None)
            if future is not None and not future.done():
                future.set_result(vector)

    def _cache_get(self, text: str) -> tuple[float, ...] | None:
        if self._cache_size <= 0:
            return None
        try:
            value = self._cache[text]
        except KeyError:
            return None
        self._cache.move_to_end(text)
        return value

    def _cache_put(self, text: str, vector: tuple[float, ...]) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


__all__ = ["CoalescingEmbeddingModel"]
