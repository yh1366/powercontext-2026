"""Static sanity check for dify/memory_tavern.yml (no Dify required).

Verifies before import that:
  1. The file is valid YAML with app/workflow/graph structure.
  2. Every edge connects existing nodes (no dangling references).
  3. The world-memory path exists: http-recall -> http-world -> format -> LLM,
     and the LLM prompt actually consumes world_context and memory_context.

Usage:
    python scripts/check_dsl.py
"""

from __future__ import annotations

import os
import sys

import yaml

DSL_PATH = os.path.join(os.path.dirname(__file__), "..", "memory_tavern.yml")

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def main() -> int:
    print("=== Memory Tavern DSL sanity check ===\n")
    with open(DSL_PATH, encoding="utf-8") as f:
        dsl = yaml.safe_load(f)

    check("valid YAML app structure", isinstance(dsl, dict) and "app" in dsl and "workflow" in dsl)
    graph = dsl["workflow"]["graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]
    check("graph has nodes", len(nodes) >= 6, f"{len(nodes)} nodes, {len(edges)} edges")

    dangling = [e["id"] for e in edges if e["source"] not in nodes or e["target"] not in nodes]
    check("all edges connect existing nodes", not dangling, f"dangling={dangling}")

    types = {nid: n["data"]["type"] for nid, n in nodes.items()}
    check("has start/llm/answer nodes",
          {"start", "llm", "answer"}.issubset(set(types.values())))

    http_nodes = [nid for nid, t in types.items() if t == "http-request"]
    check("has player/world/remember HTTP nodes",
          {"http-recall", "http-world", "http-remember"}.issubset(set(http_nodes)),
          f"{http_nodes}")

    world_node = nodes.get("http-world", {})
    world_url = world_node.get("data", {}).get("url", "")
    check("world node calls /api/recall-world", "recall-world" in world_url, world_url)

    chain_ok = any(
        e["source"] == "http-recall" and e["target"] == "http-world" for e in edges
    ) and any(
        e["source"] == "http-world" and e["target"] == "code-format-memory" for e in edges
    )
    check("workflow chain includes world recall", chain_ok)

    fmt_vars = nodes["code-format-memory"]["data"]["variables"]
    selectors = {v["variable"]: tuple(v["value_selector"]) for v in fmt_vars}
    check("format node consumes http-recall body", selectors.get("recall_response") == ("http-recall", "body"))
    check("format node consumes http-world body", selectors.get("world_response") == ("http-world", "body"))

    llm_text = nodes["llm-npc-response"]["data"]["prompt_template"][0]["text"]
    check("LLM prompt uses world_context", "{{#code-format-memory.world_context#}}" in llm_text)
    check("LLM prompt uses player memory_context", "{{#code-format-memory.memory_context#}}" in llm_text)

    remember_body = nodes["http-remember"]["data"]["body"]["data"]
    check("remember node writes via /api/remember", "/api/remember" in nodes["http-remember"]["data"]["url"])
    check("remember body carries dialogue text", "{{#sys.query#}}" in remember_body
          and "{{#llm-npc-response.text#}}" in remember_body)

    if _failures:
        print(f"\n=== RESULT: {len(_failures)} check(s) FAILED: {_failures} ===")
        return 1
    print("\n=== RESULT: all checks PASSED — DSL is internally consistent and ready to import ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
