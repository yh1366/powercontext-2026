"""HTTP-based Memory reranker with batching and score validation."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Rerank Memory candidates via an HTTP scoring endpoint."""

    policy_id = "powercontext.memory.rerank.http.v1"

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self._url = url
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        _validate_bounds(len(candidates), limit)

        scores: dict[int, float] = {}
        request_count = 0

        for batch_start in range(0, len(candidates), self._batch_size):
            batch = candidates[batch_start : batch_start + self._batch_size]
            batch_payload = [
                {"rank": batch_start + i + 1, "text": hit.text}
                for i, hit in enumerate(batch)
            ]
            request_body = {"query": query, "candidates": batch_payload}
            response = await self._client.post(
                self._url,
                content=json.dumps(request_body).encode(),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            request_count += 1

            body = _parse_response(response)
            for item in body["scores"]:
                rank = item["rank"]
                score = item["score"]
                if not isinstance(rank, int) or isinstance(rank, bool):
                    raise ValueError("score rank must be an integer")
                if rank < 1 or rank > len(candidates):
                    raise ValueError(f"score rank {rank} out of range [1, {len(candidates)}]")
                if rank in scores:
                    raise ValueError(f"duplicate rank {rank} in response")
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise ValueError("score must be a number")
                if math.isnan(score) or math.isinf(score):
                    raise ValueError("score must be finite")
                scores[rank] = float(score)

        if len(scores) != len(candidates):
            missing = set(range(1, len(candidates) + 1)) - set(scores.keys())
            raise ValueError(f"response missing scores for ranks: {sorted(missing)}")

        ordered = sorted(scores.keys(), key=lambda r: (-scores[r], r))
        selected = tuple(ordered[:limit])

        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=request_count),
            discarded_rank_count=len(candidates) - limit,
        )


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("reranker response is not valid JSON") from error

    if not isinstance(body, dict):
        raise ValueError("reranker response must be a JSON object")

    expected_keys = {"scores"}
    extra_keys = set(body.keys()) - expected_keys
    if extra_keys:
        raise ValueError(f"reranker response has unexpected fields: {sorted(extra_keys)}")
    if "scores" not in body:
        raise ValueError("reranker response missing 'scores' field")

    scores = body["scores"]
    if not isinstance(scores, list):
        raise ValueError("reranker 'scores' must be an array")

    for i, item in enumerate(scores):
        if not isinstance(item, dict):
            raise ValueError(f"scores[{i}] must be an object")
        item_expected = {"rank", "score"}
        item_extra = set(item.keys()) - item_expected
        if item_extra:
            raise ValueError(f"scores[{i}] has unexpected fields: {sorted(item_extra)}")
        if "rank" not in item or "score" not in item:
            raise ValueError(f"scores[{i}] missing required fields")

    return body


__all__ = ["HTTPMemoryReranker"]
