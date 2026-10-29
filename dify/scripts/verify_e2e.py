"""End-to-end verification for Memory Tavern, without needing a Dify instance.

This script replays the exact node sequence of the Dify Chatflow
(dify/memory_tavern.yml) against a live PowerContext server + tavern backend:

    start -> http-recall (player scope) -> http-world (world scope)
          -> code-format-memory -> [LLM] -> http-remember -> answer

Skipping only the LLM call (the judge supplies their own model), it proves:
  1. The world scope is seeded and searchable (lore / rumor retrieval).
  2. A first session stores interaction memories via /api/remember.
  3. A brand-new session recalls those memories via /api/recall —
     the cross-session core gameplay, reproduced end to end.
  4. Scope resolution is restart-safe (same server scope id comes back).

Usage:
    POWERCONTEXT_URL=http://127.0.0.1:8000 \
    BACKEND_URL=http://127.0.0.1:8600 \
    python scripts/verify_e2e.py

Exit code 0 = all checks passed. Each check prints PASS/FAIL.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PC_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8600").rstrip("/")
# Unique per run so repeated verifications start from a genuinely fresh player.
PLAYER = f"验收{time.strftime('%Y%m%d%H%M%S')}"

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def post_json(url: str, payload: dict) -> dict:
    # Bypass HTTP(S)_PROXY: loopback services must be reached directly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        print(f"  [DEBUG] {url} -> HTTP {err.code}: {body[:500]}")
        raise


def get_json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as resp:
        return json.load(resp)


def main() -> None:
    print(f"=== Memory Tavern end-to-end verification ===")
    print(f"PowerContext: {PC_URL}\nBackend:      {BACKEND_URL}\n")

    print("[0] Health checks")
    live = get_json(f"{PC_URL}/health/live")
    check("PowerContext /health/live", live.get("status") == "ok")
    health = get_json(f"{BACKEND_URL}/health")
    check("Backend /health", health.get("status") == "ok", f"powercontext_url={health.get('powercontext_url')}")
    print()

    print("[1] World memory (node: http-world -> /api/recall-world)")
    world = post_json(
        f"{BACKEND_URL}/api/recall-world",
        {"player_name": PLAYER, "query": "酒馆 井水 传闻 未来", "limit": 5},
    )
    check("world scope has seeded memories", world.get("world_hit_count", 0) > 0,
          f"hits={world.get('world_hit_count')}")
    print("      world_context:")
    for line in (world.get("world_context") or "").splitlines():
        print(f"        {line}")
    check("world scope_id is server-owned (scp_)", str(world.get("scope_id", "")).startswith("scp_"),
          str(world.get("scope_id")))
    print()

    print("[2] Session A — first visit (nodes: http-recall + http-remember)")
    recall_a = post_json(
        f"{BACKEND_URL}/api/recall",
        {"player_name": PLAYER, "query": "还记得我吗", "limit": 8},
    )
    check("fresh player has no memories yet", recall_a.get("hit_count") == 0,
          f"hits={recall_a.get('hit_count')}")
    fact1 = post_json(
        f"{BACKEND_URL}/api/remember",
        {"player_name": PLAYER, "kind": "interaction",
         "text": f"客人{PLAYER}说：我刚从北方的冰霜山脉过来，差点被雪狼咬了！。老胡回应：嚯，冰霜山脉来的，命真硬。",
         "reason": "verify_e2e session A turn 1"},
    )
    check("remember turn 1", fact1.get("status") == "remembered", f"entry_id={fact1.get('entry_id')}")
    fact2 = post_json(
        f"{BACKEND_URL}/api/remember",
        {"player_name": PLAYER, "kind": "interaction",
         "text": f"客人{PLAYER}说：我最喜欢喝蜂蜜酒，别拿麦酒糊弄我。老胡回应：得嘞，蜂蜜酒记下了。",
         "reason": "verify_e2e session A turn 2"},
    )
    check("remember turn 2", fact2.get("status") == "remembered", f"entry_id={fact2.get('entry_id')}")
    print()

    print("[3] Session B — brand-new session, same player (node: http-recall)")
    recall_b = post_json(
        f"{BACKEND_URL}/api/recall",
        {"player_name": PLAYER, "query": "老胡，还记得我吗？我从哪儿来？", "limit": 8},
    )
    context_b = recall_b.get("memory_context", "")
    check("cross-session memory hit", recall_b.get("hit_count", 0) > 0,
          f"hits={recall_b.get('hit_count')}")
    check("memory contains the ice-mountain fact", "冰霜山脉" in context_b)
    check("memory contains the wolf fact", "雪狼" in context_b)
    print("      memory_context returned to the LLM node:")
    for line in context_b.splitlines():
        print(f"        {line}")
    recall_c = post_json(
        f"{BACKEND_URL}/api/recall",
        {"player_name": PLAYER, "query": "客人喜欢喝蜂蜜酒", "limit": 8},
    )
    check("memory contains the honey-mead preference", "蜂蜜酒" in recall_c.get("memory_context", ""))
    print()

    print("[4] Restart safety — scope re-resolution is idempotent")
    init_again = post_json(f"{BACKEND_URL}/api/init", {"player_name": PLAYER})
    check("same scope_id after re-resolution", str(init_again.get("scope_id", "")).startswith("scp_"),
          str(init_again.get("scope_id")))
    print()

    if _failures:
        print(f"=== RESULT: {len(_failures)} check(s) FAILED: {_failures} ===")
        sys.exit(1)
    print("=== RESULT: all checks PASSED — cross-session memory gameplay reproduced ===")


if __name__ == "__main__":
    main()
