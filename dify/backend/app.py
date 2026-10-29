"""
Memory Tavern - PowerContext bridge service for Dify.

Dify HTTP Request nodes call these endpoints; the bridge talks to the
PowerContext HTTP API:

- POST /api/recall        -> /v1/memory/search on the player scope
- POST /api/recall-world  -> /v1/memory/search on the shared NPC world scope
- POST /api/remember      -> /v1/memory/remember on the player scope
- POST /api/init          -> create/resolve a player scope

Scope model (per PowerContext HTTP API docs):

- The Server owns Scope IDs (``scp_...``). Clients create a Scope with a
  stable ``idempotency_key`` and keep the returned ``scope_id``.
- Re-POSTing /v1/scopes with the same idempotency_key returns the original
  Scope, so the bridge can always re-resolve its Scopes after a restart.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

POWERCONTEXT_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_TOKEN = os.getenv("POWERCONTEXT_TOKEN", "")
WORLD_SCOPE_KEY = os.getenv("TAVERN_WORLD_SCOPE_KEY", "tavern-npc-world")

logger = logging.getLogger("memory_tavern")

app = FastAPI(title="Memory Tavern Bridge", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: httpx.AsyncClient | None = None
_scope_cache: dict[str, str] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # trust_env=False: never route loopback PowerContext calls through
        # HTTP(S)_PROXY environment variables — a proxy would break the API.
        _client = httpx.AsyncClient(base_url=POWERCONTEXT_URL, timeout=15.0, trust_env=False)
    return _client


def _auth_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if POWERCONTEXT_TOKEN:
        headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    return headers


def _player_scope_key(player_name: str) -> str:
    """Stable logical key for a player; used as the Scope idempotency_key."""
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", player_name.lower()).strip("-")
    slug = slug[:40] or "anonymous"
    return f"tavern-player-{slug}"


async def resolve_scope(logical_key: str, title: str, summary: str) -> str:
    """Create (once) and resolve the Server-owned Scope ID for a logical key.

    PowerContext returns a server-generated ``scp_...`` scope_id. Posting the
    same idempotency_key again is idempotent and returns the same Scope, so
    this is safe to call on every request and after restarts.
    """
    cached = _scope_cache.get(logical_key)
    if cached:
        return cached

    client = _get_client()
    resp = await client.post(
        "/v1/scopes",
        headers=_auth_headers(),
        json={"title": title, "summary": summary, "idempotency_key": logical_key},
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"PowerContext scope resolve failed for {logical_key}: {resp.text}",
        )
    scope_id = resp.json().get("scope_id", "")
    if not scope_id:
        raise HTTPException(status_code=502, detail="PowerContext returned no scope_id")
    _scope_cache[logical_key] = scope_id
    logger.info("Resolved scope %s -> %s", logical_key, scope_id)
    return scope_id


async def search_memories(scope_id: str, query: str, limit: int) -> list[dict[str, Any]]:
    """Search one scope; returns [{kind, text, entry_id}] (empty on 404)."""
    client = _get_client()
    resp = await client.post(
        "/v1/memory/search",
        headers=_auth_headers(),
        json={"scope_id": scope_id, "query": query, "limit": limit},
    )
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PowerContext memory/search failed: {resp.text}",
        )
    hits = resp.json().get("hits", []) or []
    results: list[dict[str, Any]] = []
    for hit in hits:
        # Search hits carry text/kind at the top level (verified against the
        # 2026-competition-ecosystem HTTP API).
        results.append(
            {
                "kind": hit.get("kind", "fact"),
                "text": hit.get("text", ""),
                "entry_id": hit.get("citation", {}).get("entry_id", ""),
            }
        )
    return results


def _format_lines(hits: list[dict[str, Any]]) -> list[str]:
    return [f"[{h['kind']}] {h['text']}" for h in hits]


class RecallRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class RememberRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(default="interaction", min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=4000)
    reason: str | None = Field(default=None, max_length=200)


class InitRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)


@app.on_event("startup")
async def startup() -> None:
    try:
        await resolve_scope(
            WORLD_SCOPE_KEY,
            "Memory Tavern NPC World",
            "Shared world knowledge for the Memory Tavern NPC: personality, lore, rules, rumors.",
        )
        logger.info("NPC world scope ready: %s", WORLD_SCOPE_KEY)
    except Exception as exc:  # noqa: BLE001 - startup must not crash the bridge
        logger.warning("Could not create NPC world scope at startup: %s", exc)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "powercontext_url": POWERCONTEXT_URL}


@app.post("/api/init")
async def init_player(req: InitRequest) -> dict[str, str]:
    key = _player_scope_key(req.player_name)
    scope_id = await resolve_scope(
        key,
        f"Tavern Player: {req.player_name}",
        f"Per-player memory for {req.player_name} in the Memory Tavern.",
    )
    return {"scope_key": key, "scope_id": scope_id, "status": "initialized"}


@app.post("/api/recall")
async def recall_player_memories(req: RecallRequest) -> dict[str, Any]:
    """Recall this player's own memories (per-player scope)."""
    key = _player_scope_key(req.player_name)
    scope_id = await resolve_scope(
        key,
        f"Tavern Player: {req.player_name}",
        f"Per-player memory for {req.player_name} in the Memory Tavern.",
    )
    hits = await search_memories(scope_id, req.query, req.limit)
    return {
        "scope_key": key,
        "scope_id": scope_id,
        "hit_count": len(hits),
        "memory_context": "\n".join(_format_lines(hits)),
        "hits": hits,
    }


@app.post("/api/recall-world")
async def recall_world_memories(req: RecallRequest) -> dict[str, Any]:
    """Recall shared world knowledge (lore / personality / rules / rumors)."""
    scope_id = await resolve_scope(
        WORLD_SCOPE_KEY,
        "Memory Tavern NPC World",
        "Shared world knowledge for the Memory Tavern NPC: personality, lore, rules, rumors.",
    )
    hits = await search_memories(scope_id, req.query, min(req.limit, 5))
    return {
        "scope_key": WORLD_SCOPE_KEY,
        "scope_id": scope_id,
        "world_hit_count": len(hits),
        "world_context": "\n".join(_format_lines(hits)),
        "hits": hits,
    }


@app.post("/api/remember")
async def remember_interaction(req: RememberRequest) -> dict[str, str]:
    key = _player_scope_key(req.player_name)
    scope_id = await resolve_scope(
        key,
        f"Tavern Player: {req.player_name}",
        f"Per-player memory for {req.player_name} in the Memory Tavern.",
    )
    # PowerContext entry limit: 8192 UTF-8 bytes.
    text = req.text.encode("utf-8")[:8000].decode("utf-8", errors="ignore")
    client = _get_client()
    resp = await client.post(
        "/v1/memory/remember",
        headers=_auth_headers(),
        json={
            "scope_id": scope_id,
            "kind": req.kind,
            "text": text,
            "reason": req.reason or "Tavern interaction",
        },
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PowerContext memory/remember failed: {resp.text}",
        )
    data = resp.json() or {}
    entry = data.get("entry") or {}
    citation = entry.get("citation") or {} if isinstance(entry, dict) else {}
    entry_id = citation.get("entry_id", "")
    return {"scope_key": key, "scope_id": scope_id, "entry_id": entry_id, "status": "remembered"}


@app.post("/api/seed-world")
async def seed_world_memories() -> dict[str, str]:
    """Seed the shared world scope (idempotent: same scope, additive entries)."""
    scope_id = await resolve_scope(
        WORLD_SCOPE_KEY,
        "Memory Tavern NPC World",
        "Shared world knowledge for the Memory Tavern NPC: personality, lore, rules, rumors.",
    )
    seeds = [
        ("lore", "记忆酒馆坐落在七个国度的十字路口，酒窖比它所在的山还古老。"),
        ("lore", "掌柜老胡曾招待过一位扮成商人的龙，龙留下了一枚金鳞当小费，至今挂在吧台上方。"),
        ("personality", "老胡粗鲁但公道：敬重诚实的人，鄙视说谎的人，对落魄冒险家和流浪诗人格外心软。"),
        ("personality", "老胡从不忘任何一张脸，也从不忘记一笔账。他有一本传奇的恩怨本，记下每次冒犯、每份善意和每个没结的账单。"),
        ("rule", "酒馆铁律：吧台前不许带武器。老胡亲自用柜台下的弩执行这条规矩。"),
        ("rule", "常客打九折，第一次来的客人全价——除非能讲个好故事。"),
        ("rumor", "有人说酒馆的井水能看见未来。老胡否认，但井水照卖双倍价钱。"),
        ("rumor", "传闻楼上闹鬼。老胡说那只是风声，但他从不让人在楼上过夜。"),
    ]
    client = _get_client()
    saved = 0
    for kind, text in seeds:
        resp = await client.post(
            "/v1/memory/remember",
            headers=_auth_headers(),
            json={
                "scope_id": scope_id,
                "kind": kind,
                "text": text,
                "reason": "World seed data for Memory Tavern",
            },
        )
        if resp.status_code == 200:
            saved += 1
    return {"status": "seeded", "scope_id": scope_id, "count": str(saved)}
