#!/usr/bin/env python3
"""Regenerate the filtered GET-only catalog from the full ConnectWise spec.

The full "All endpoints" OpenAPI spec is not committed (it's ~15 MB); download
it from https://developer.connectwise.com (Manage REST API reference exposes
it as All.json), then run:

    python scripts/generate_catalog.py path/to/All.json

Scope is taken from connectwise_mcp.scope.SELECTED_CATEGORIES — edit that set
to widen/narrow what the server exposes, then rerun this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from connectwise_mcp.scope import SELECTED_CATEGORIES  # noqa: E402

OUT = SRC / "connectwise_mcp" / "data" / "openapi_get_filtered.json"


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="Path to the full ConnectWise OpenAPI JSON")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())

    # Keep only GET operations whose tag is in scope.
    paths = {}
    for path, item in spec["paths"].items():
        op = item.get("get")
        if op and set(op.get("tags", [])) & SELECTED_CATEGORIES:
            paths[path] = {"get": op}

    # Prune schemas to those reachable from the kept operations.
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
            "title": spec["info"]["title"] + " (GET subset)",
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
    print(
        f"Wrote {OUT}: {len(paths)} GET operations, "
        f"{len(filtered['components']['schemas'])} schemas "
        f"({len(SELECTED_CATEGORIES)} categories in scope)"
    )


if __name__ == "__main__":
    main()
