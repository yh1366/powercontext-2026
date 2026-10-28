"""Initialize PowerContext scopes and seed world data for Memory Tavern."""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"  [WARN] {path} -> HTTP {e.code}: {detail[:200]}")
        return {}


def ensure_scope(scope_id: str, title: str, summary: str) -> None:
    result = post("/v1/scopes", {
        "title": title,
        "summary": summary,
        "idempotency_key": scope_id,
    })
    sid = result.get("scope_id", scope_id)
    print(f"  Scope ready: {sid}")


def remember(scope_id: str, kind: str, text: str, reason: str = "") -> None:
    post("/v1/memory/remember", {
        "scope_id": scope_id,
        "kind": kind,
        "text": text,
        "reason": reason or "Seed data",
    })


def main() -> None:
    print("=== Memory Tavern: PowerContext Initialization ===\n")

    print("[1/3] Checking PowerContext connection...")
    try:
        with urlopen(f"{BASE_URL}/health/live", timeout=5) as resp:
            print(f"  PowerContext is live at {BASE_URL}\n")
    except Exception as e:
        print(f"  ERROR: Cannot reach PowerContext at {BASE_URL}")
        print(f"  {e}")
        print("  Make sure PowerContext server is running:")
        print("    powercontext server run")
        sys.exit(1)

    print("[2/3] Creating NPC world scope...")
    ensure_scope(
        "tavern-npc-world",
        "Memory Tavern NPC World",
        "Shared world knowledge for the Memory Tavern NPC.",
    )
    print()

    print("[3/3] Seeding world memories...")
    seeds = [
        ("lore", "The Memory Tavern sits at the crossroads of seven realms. Travelers say its cellar is older than the mountain it rests upon."),
        ("lore", "Old Hu, the tavern keeper, once served a dragon disguised as a merchant. The dragon left a single golden scale as tip - it still hangs above the bar."),
        ("personality", "Old Hu is gruff but fair. He respects honesty and despises liars. He has a soft spot for wandering poets and broke adventurers."),
        ("personality", "Old Hu never forgets a face - or a debt. He keeps a legendary grudge book where he records every slight, every kindness, and every unpaid tab."),
        ("rule", "The tavern has one iron rule: no weapons at the bar. Old Hu enforces it personally with a crossbow mounted under the counter."),
        ("rule", "Regulars get a 10% discount. First-timers pay full price unless they can tell a good story."),
        ("rumor", "Some say the tavern's well water grants visions of the future. Old Hu denies this but charges double for well water anyway."),
        ("rumor", "A ghost is rumored to haunt the upstairs room. Old Hu claims it's just the wind, but he refuses to let anyone stay there overnight."),
    ]
    for kind, text in seeds:
        remember("tavern-npc-world", kind, text, "World seed data")
        print(f"  Seeded [{kind}]: {text[:60]}...")

    print("\n=== Done! Memory Tavern world is ready. ===")
    print("You can now import the Dify DSL and start chatting with Old Hu.")


if __name__ == "__main__":
    main()
