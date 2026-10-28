"""
Memory Tavern - PowerContext bridge service for Dify.

Provides REST endpoints that Dify HTTP Request nodes call to
recall and save NPC memories via the PowerContext HTTP API.
"""

from __future__ import annotations

import json
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
SCOPE_PREFIX = "tavern"
NPC_SCOPE_ID = f"{SCOPE_PREFIX}-npc-world"

logger = logging.getLogger("memory_tavern")

app = FastAPI(title="Memory Tavern Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=POWERCONTEXT_URL, timeout=15.0)
    return _client


def _auth_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if POWERCONTEXT_TOKEN:
        headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    return headers


def _player_scope_id(player_name: str) -> str:
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", player_name.lower()).strip("-")
    slug = slug[:40] or "anonymous"
    return f"{SCOPE_PREFIX}-player-{slug}"


class RecallRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class RememberRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=4000)
    reason: str | None = Field(default=None, max_length=200)


class InitRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)


class RecallResponse(BaseModel):
    scope_id: str
    memory_context: str
    hit_count: int
    hits: list[dict[str, Any]]


class RememberResponse(BaseModel):
    scope_id: str
    entry_id: str
    status: str


async def _ensure_scope(scope_id: str, title: str, summary: str) -> None:
    client = _get_client()
    resp = await client.post(
        "/v1/scopes",
        headers=_auth_headers(),
        json={
            "title": title,
            "summary": summary,
            "idempotency_key": scope_id,
        },
    )
    if resp.status_code not in (200, 201, 409):
        detail = resp.text
        raise HTTPException(status_code=502, detail=f"PowerContext scope creation failed: {detail}")


@app.on_event("startup")
async def startup() -> None:
    try:
        await _ensure_scope(
            NPC_SCOPE_ID,
            "Memory Tavern NPC World",
            "Shared world knowledge for the Memory Tavern NPC, including personality, lore, and rules.",
        )
        logger.info("NPC world scope ready: %s", NPC_SCOPE_ID)
    except Exception as exc:
        logger.warning("Could not create NPC world scope at startup: %s", exc)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "powercontext_url": POWERCONTEXT_URL}


@app.post("/api/init", response_model=RememberResponse)
async def init_player(req: InitRequest) -> RememberResponse:
    scope_id = _player_scope_id(req.player_name)
    await _ensure_scope(
        scope_id,
        f"Tavern Player: {req.player_name}",
        f"Memory scope for player {req.player_name} in the Memory Tavern.",
    )
    return RememberResponse(scope_id=scope_id, entry_id="", status="initialized")


@app.post("/api/recall", response_model=RecallResponse)
async def recall_memories(req: RecallRequest) -> RecallResponse:
    scope_id = _player_scope_id(req.player_name)
    client = _get_client()

    resp = await client.post(
        "/v1/memory/search",
        headers=_auth_headers(),
        json={
            "scope_id": scope_id,
            "query": req.query,
            "limit": req.limit,
        },
    )

    if resp.status_code == 404:
        return RecallResponse(
            scope_id=scope_id,
            memory_context="",
            hit_count=0,
            hits=[],
        )

    if resp.status_code != 200:
        detail = resp.text
        raise HTTPException(status_code=502, detail=f"PowerContext search failed: {detail}")

    data = resp.json()
    hits = data.get("hits", [])

    if not hits:
        return RecallResponse(
            scope_id=scope_id,
            memory_context="(No prior memories found. This appears to be a fresh encounter.)",
            hit_count=0,
            hits=[],
        )

    lines: list[str] = []
    hit_details: list[dict[str, Any]] = []
    for i, hit in enumerate(hits, 1):
        entry = hit.get("entry", {})
        text = entry.get("text", "")
        kind = entry.get("kind", "fact")
        lines.append(f"[Memory {i}] ({kind}) {text}")
        hit_details.append({"kind": kind, "text": text, "entry_id": entry.get("entry_id", "")})

    memory_context = "\n".join(lines)
    return RecallResponse(
        scope_id=scope_id,
        memory_context=memory_context,
        hit_count=len(hits),
        hits=hit_details,
    )


@app.post("/api/remember", response_model=RememberResponse)
async def remember_fact(req: RememberRequest) -> RememberResponse:
    scope_id = _player_scope_id(req.player_name)
    client = _get_client()

    await _ensure_scope(
        scope_id,
        f"Tavern Player: {req.player_name}",
        f"Memory scope for player {req.player_name} in the Memory Tavern.",
    )

    text_bytes = req.text.encode("utf-8")
    if len(text_bytes) > 8192:
        req.text = req.text.encode("utf-8")[:8000].decode("utf-8", errors="ignore")

    resp = await client.post(
        "/v1/memory/remember",
        headers=_auth_headers(),
        json={
            "scope_id": scope_id,
            "kind": req.kind,
            "text": req.text,
            "reason": req.reason or "Tavern interaction",
        },
    )
    if resp.status_code != 200:
        detail = resp.text
        raise HTTPException(status_code=502, detail=f"PowerContext remember failed: {detail}")

    data = resp.json()
    memory_ref = data.get("memory", {})
    entry_id = memory_ref.get("artifact_id", "")

    return RememberResponse(scope_id=scope_id, entry_id=entry_id, status="remembered")


@app.post("/api/recall-world")
async def recall_world_context(req: RecallRequest) -> dict[str, Any]:
    client = _get_client()
    resp = await client.post(
        "/v1/memory/search",
        headers=_auth_headers(),
        json={
            "scope_id": NPC_SCOPE_ID,
            "query": req.query,
            "limit": min(req.limit, 5),
        },
    )
    if resp.status_code != 200:
        return {"world_context": ""}

    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        return {"world_context": ""}

    lines = []
    for hit in hits:
        entry = hit.get("entry", {})
        lines.append(f"- {entry.get('text', '')}")
    return {"world_context": "\n".join(lines)}


@app.post("/api/seed-world")
async def seed_world_memories() -> dict[str, str]:
    client = _get_client()
    seeds = [
        ("lore", "The Memory Tavern sits at the crossroads of seven realms. Travelers say its cellar is older than the mountain it rests upon."),
        ("lore", "Old Hu, the tavern keeper, once served a dragon disguised as a merchant. The dragon left a single golden scale as tip — it still hangs above the bar."),
        ("personality", "Old Hu is gruff but fair. He respects honesty and despises liars. He has a soft spot for wandering poets and broke adventurers."),
        ("personality", "Old Hu never forgets a face — or a debt. He keeps a legendary grudge book where he records every slight, every kindness, and every unpaid tab."),
        ("rule", "The tavern has one iron rule: no weapons at the bar. Old Hu enforces it personally with a crossbow mounted under the counter."),
        ("rule", "Regulars get a 10% discount. First-timers pay full price unless they can tell a good story."),
        ("rumor", "Some say the tavern's well water grants visions of the future. Old Hu denies this but charges double for well water anyway."),
        ("rumor", "A ghost is rumored to haunt the upstairs room. Old Hu claims it's just the wind, but he refuses to let anyone stay there overnight."),
    ]

    saved = 0
    for kind, text in seeds:
        resp = await client.post(
            "/v1/memory/remember",
            headers=_auth_headers(),
            json={
                "scope_id": NPC_SCOPE_ID,
                "kind": kind,
                "text": text,
                "reason": "World seed data for Memory Tavern",
            },
        )
        if resp.status_code == 200:
            saved += 1

    return {"status": "seeded", "count": str(saved)}
