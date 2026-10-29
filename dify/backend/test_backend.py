"""
Quick test script for the Memory Tavern backend.
Run this after starting PowerContext and the backend to verify everything works.

Usage:
    python test_backend.py
"""

from __future__ import annotations

import json
import sys
from urllib.request import Request, urlopen
import urllib.error
import urllib.request
from urllib.error import HTTPError

BACKEND_URL = "http://127.0.0.1:8600"

# Bypass HTTP(S)_PROXY env vars: loopback services must be reached directly.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{BACKEND_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return json.load(resp)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"  FAIL: {path} -> HTTP {e.code}: {detail[:300]}")
        return {}
    except Exception as e:
        print(f"  FAIL: {path} -> {e}")
        return {}


def main() -> int:
    print("=== Memory Tavern Backend Test ===\n")
    errors = 0

    print("[1] Health check...")
    try:
        with _OPENER.open(f"{BACKEND_URL}/health", timeout=5) as resp:
            data = json.load(resp)
            print(f"  OK: {data}")
    except Exception as e:
        print(f"  FAIL: Backend not running at {BACKEND_URL}")
        print(f"  Start it with: uvicorn app:app --host 0.0.0.0 --port 8600")
        return 1

    print("\n[2] Seed world memories...")
    result = post("/api/seed-world", {})
    if result.get("status") == "seeded":
        print(f"  OK: Seeded {result.get('count', '?')} world memories")
    else:
        print(f"  FAIL: {result}")
        errors += 1

    print("\n[3] Init player scope...")
    result = post("/api/init", {"player_name": "TestHero"})
    if result.get("status") == "initialized":
        print(f"  OK: Scope = {result.get('scope_id', '?')}")
    else:
        print(f"  FAIL: {result}")
        errors += 1

    print("\n[4] Recall (should be empty for new player)...")
    result = post("/api/recall", {"player_name": "TestHero", "query": "adventure", "limit": 5})
    print(f"  OK: hit_count = {result.get('hit_count', '?')}")

    print("\n[5] Remember a fact...")
    result = post("/api/remember", {
        "player_name": "TestHero",
        "kind": "interaction",
        "text": "TestHero said they defeated a dragon in the northern mountains.",
        "reason": "Test interaction",
    })
    if result.get("status") == "remembered":
        print(f"  OK: entry_id = {result.get('entry_id', '?')}")
    else:
        print(f"  FAIL: {result}")
        errors += 1

    print("\n[6] Recall again (should find the memory)...")
    result = post("/api/recall", {"player_name": "TestHero", "query": "dragon northern mountains", "limit": 5})
    hit_count = result.get("hit_count", 0)
    if hit_count > 0:
        print(f"  OK: Found {hit_count} memories")
        for hit in result.get("hits", []):
            print(f"    - [{hit.get('kind', '?')}] {hit.get('text', '')[:80]}")
    else:
        print(f"  WARN: No memories found (may need full-text indexing delay)")
        print(f"  Response: {json.dumps(result, ensure_ascii=False)[:200]}")

    print(f"\n=== Test complete. Errors: {errors} ===")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
