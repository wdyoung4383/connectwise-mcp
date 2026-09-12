#!/usr/bin/env python3
"""Regenerate the bundled catalog from the full ConnectWise spec.

The full "All endpoints" OpenAPI spec is not committed (it is ~8 MB); download
it from https://developer.connectwise.com (the Manage REST API reference
exposes it as All.json), then run:

    python scripts/generate_catalog.py path/to/All.json

Scope comes from connectwise_mcp.scope: SELECTED_CATEGORIES (tags, or None for
all) and INCLUDED_METHODS (HTTP verbs). Edit those, then rerun this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from connectwise_mcp.scope import (  # noqa: E402
    INCLUDED_METHODS,
    SELECTED_CATEGORIES,
)

OUT = SRC / "connectwise_mcp" / "data" / "openapi_filtered.json"


def collect_refs(obj, acc: set[str]) -> None:
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            acc.add(ref.split("/")[-1])
        for v in obj.values():
            collect_refs(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_refs(v, acc)


def in_scope(op: dict) -> bool:
    if SELECTED_CATEGORIES is None:
        return True
    return bool(set(op.get("tags", [])) & SELECTED_CATEGORIES)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="Path to the full ConnectWise OpenAPI JSON")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())

    # Keep every in-scope operation, across every included verb. Unlike the
    # previous GET-only catalog, request bodies are retained too — cw_call
    # needs describe_endpoint to show the body shape for writes.
    paths: dict[str, dict] = {}
    verbs: Counter[str] = Counter()
    for path, item in spec["paths"].items():
        kept = {}
        for method, op in item.items():
            if method.lower() not in INCLUDED_METHODS:
                continue
            if not isinstance(op, dict) or not in_scope(op):
                continue
            kept[method.lower()] = op
            verbs[method.lower()] += 1
        if kept:
            paths[path] = kept

    # Prune schemas to those reachable from the kept operations (responses and
    # request bodies alike).
    schemas = spec.get("components", {}).get("schemas", {})
    seed: set[str] = set()
    collect_refs(paths, seed)
    reachable: set[str] = set()
    stack = list(seed)
    while stack:
        name = stack.pop()
        if name in reachable:
            continue
        reachable.add(name)
        if name in schemas:
            sub: set[str] = set()
            collect_refs(schemas[name], sub)
            stack.extend(sub - reachable)

    filtered = {
        "openapi": spec["openapi"],
        "info": {
            "title": spec["info"]["title"],
            "version": spec["info"]["version"],
        },
        "servers": spec.get("servers", []),
        "paths": paths,
        "components": {
            "schemas": {n: schemas[n] for n in sorted(reachable) if n in schemas},
            "securitySchemes": spec["components"].get("securitySchemes", {}),
        },
    }
    OUT.write_text(json.dumps(filtered, indent=1))

    scope_desc = (
        "all categories"
        if SELECTED_CATEGORIES is None
        else f"{len(SELECTED_CATEGORIES)} categories"
    )
    breakdown = ", ".join(f"{m.upper()} {n}" for m, n in sorted(verbs.items()))
    print(
        f"Wrote {OUT}\n"
        f"  spec version: {spec['info']['version']}\n"
        f"  {len(paths)} paths, {sum(verbs.values())} operations ({breakdown})\n"
        f"  {len(filtered['components']['schemas'])} schemas, {scope_desc}"
    )


if __name__ == "__main__":
    main()
