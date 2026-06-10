#!/usr/bin/env python3
"""Live smoke test against a real ConnectWise instance (read-only).

Requires CW_* env vars (see .env.example). Run:

    python scripts/live_smoke.py

Exercises auth, plain lists, conditions filtering (strings, booleans, dates,
nested fields), field projection, pagination, and a by-id fetch. Read-only:
only GETs are issued.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from connectwise_mcp.auth import get_credentials  # noqa: E402
from connectwise_mcp.catalog import load_catalog  # noqa: E402
from connectwise_mcp.client import make_client  # noqa: E402
from connectwise_mcp.executor import ExecutionError, cw_get  # noqa: E402

WEEK_AGO = "[" + (
    datetime.now(timezone.utc) - timedelta(days=7)
).strftime("%Y-%m-%dT%H:%M:%SZ") + "]"

CHECKS = [
    ("plain list", "/service/boards", {}),
    ("string condition", "/company/companies", {"conditions": "deletedFlag = false"}),
    (
        "nested + boolean",
        "/service/tickets",
        {"conditions": "closedFlag = false", "order_by": "id desc"},
    ),
    (
        "date condition",
        "/service/tickets",
        {"conditions": f"lastUpdated > {WEEK_AGO}"},
    ),
    (
        "field projection",
        "/company/companies",
        {"fields": "id,identifier,name", "page_size": 5},
    ),
    ("pagination page 2", "/company/companies", {"page": 2, "page_size": 5}),
    ("count endpoint", "/service/tickets/count", {}),
]


async def main() -> int:
    creds = get_credentials()
    catalog = load_catalog()
    failures = 0
    async with make_client(creds) as client:
        for name, path, kwargs in CHECKS:
            try:
                out = await cw_get(client, catalog, path, **kwargs)
                if isinstance(out, dict) and "items" in out:
                    detail = f"{out['count']} items, has_more={out['has_more']}"
                else:
                    detail = str(out)[:80]
                print(f"  OK   {name:20s} {path}  ->  {detail}")
            except ExecutionError as e:
                failures += 1
                print(f"  FAIL {name:20s} {path}  ->  {e}")

        # by-id round trip: take the first ticket from the list, fetch it
        try:
            listing = await cw_get(
                client, catalog, "/service/tickets", page_size=1
            )
            items = listing.get("items") or []
            if items:
                tid = items[0]["id"]
                t = await cw_get(
                    client, catalog, "/service/tickets/{id}", path_params={"id": tid}
                )
                print(f"  OK   by-id fetch         /service/tickets/{tid}  ->  "
                      f"'{str(t.get('summary'))[:50]}'")
            else:
                print("  SKIP by-id fetch (no tickets visible to this member)")
        except ExecutionError as e:
            failures += 1
            print(f"  FAIL by-id fetch  ->  {e}")

    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
