"""Initialize PowerContext scopes and seed world data for Memory Tavern.

Scope model (per PowerContext HTTP API docs):
- The Server owns Scope IDs (``scp_...``). Clients create a Scope with a
  stable ``idempotency_key`` and keep the returned ``scope_id``.
- Re-POSTing /v1/scopes with the same idempotency_key is idempotent and
  returns the original Scope, so this script can be re-run safely.

Usage:
    POWERCONTEXT_URL=http://127.0.0.1:8000 python scripts/init_powercontext.py
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("POWERCONTEXT_TOKEN", "")
WORLD_SCOPE_KEY = "tavern-npc-world"

SEEDS = [
    ("lore", "记忆酒馆坐落在七个国度的十字路口，酒窖比它所在的山还古老。"),
    ("lore", "掌柜老胡曾招待过一位扮成商人的龙，龙留下了一枚金鳞当小费，至今挂在吧台上方。"),
    ("personality", "老胡粗鲁但公道：敬重诚实的人，鄙视说谎的人，对落魄冒险家和流浪诗人格外心软。"),
    ("personality", "老胡从不忘任何一张脸，也从不忘记一笔账。他有一本传奇的恩怨本，记下每次冒犯、每份善意和每个没结的账单。"),
    ("rule", "酒馆铁律：吧台前不许带武器。老胡亲自用柜台下的弩执行这条规矩。"),
    ("rule", "常客打九折，第一次来的客人全价——除非能讲个好故事。"),
    ("rumor", "有人说酒馆的井水能看见未来。老胡否认，但井水照卖双倍价钱。"),
    ("rumor", "传闻楼上闹鬼。老胡说那只是风声，但他从不让人在楼上过夜。"),
]


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = Request(f"{BASE_URL}{path}", data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"  [WARN] {path} -> HTTP {e.code}: {detail[:300]}")
        return {}


def resolve_scope(logical_key: str, title: str, summary: str) -> str:
    result = post("/v1/scopes", {"title": title, "summary": summary, "idempotency_key": logical_key})
    scope_id = result.get("scope_id", "")
    if not scope_id:
        print(f"  ERROR: could not resolve scope for {logical_key}")
        sys.exit(1)
    print(f"  Scope {logical_key} -> {scope_id}")
    return scope_id


def remember(scope_id: str, kind: str, text: str, reason: str = "") -> bool:
    result = post(
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": kind, "text": text, "reason": reason or "Seed data"},
    )
    return bool(result)


def main() -> None:
    print("=== Memory Tavern: PowerContext Initialization ===\n")

    print("[1/3] Checking PowerContext connection...")
    try:
        with urlopen(f"{BASE_URL}/health/live", timeout=5) as resp:
            print(f"  PowerContext is live at {BASE_URL}\n")
    except Exception as e:
        print(f"  ERROR: Cannot reach PowerContext at {BASE_URL}")
        print(f"  {e}")
        print("  Make sure the PowerContext server is running first.")
        sys.exit(1)

    print("[2/3] Resolving NPC world scope (idempotency_key=%s)..." % WORLD_SCOPE_KEY)
    world_scope_id = resolve_scope(
        WORLD_SCOPE_KEY,
        "Memory Tavern NPC World",
        "Shared world knowledge for the Memory Tavern NPC: personality, lore, rules, rumors.",
    )
    print()

    print("[3/3] Seeding world memories...")
    seeded = 0
    for kind, text in SEEDS:
        if remember(world_scope_id, kind, text, "World seed data"):
            seeded += 1
            print(f"  Seeded [{kind}]: {text[:40]}...")
        else:
            print(f"  [WARN] failed to seed [{kind}]: {text[:40]}...")
    print(f"  {seeded}/{len(SEEDS)} world memories written to scope {world_scope_id}")

    print("\n=== Done! Memory Tavern world is ready. ===")
    print("Next: import dify/memory_tavern.yml into Dify, start the backend, and chat.")


if __name__ == "__main__":
    main()
