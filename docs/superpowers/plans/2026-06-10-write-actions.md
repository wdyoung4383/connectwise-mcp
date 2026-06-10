# Write Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five curated create actions (ticket, time entry, ticket note, company, contact) to the read-only ConnectWise MCP server, gated by a hard-coded POST allowlist.

**Architecture:** A new `writer.py` module holds the only POST code path, restricted to five allowlisted paths with POST-safe retry semantics (never retry 5xx/read-timeouts). A new `curated_writes.py` holds five MCP tools that resolve friendly names (company, board, member, contact) to IDs via the existing read executor, build minimal bodies, and POST. `server.py` registers them unconditionally.

**Tech Stack:** Python 3.10+, FastMCP, httpx (MockTransport for tests), pytest + pytest-asyncio (asyncio_mode=auto).

**Spec:** `docs/superpowers/specs/2026-06-10-write-actions-design.md`

**Working directory:** `C:\Automation\Mann IT\connectwise-mcp\connectwise-mcp` (all paths below relative to it). Run tests with the project venv: `.venv\Scripts\python.exe -m pytest`.

**Verified API contract facts** (from `src/connectwise_mcp/data/openapi_get_filtered.json` schemas — do not re-litigate):
- `TimeEntry.chargeToType` enum: `["Company", "ServiceTicket", "ProjectTicket", "ChargeCode", "Activity"]`; if `chargeToId` is unset CW charges to `company`.
- `Ticket` required: `summary`, `company`. `Company` required: `identifier`, `name` (no site needed on create). `ServiceNote`/`Contact`: no required fields enforced by schema.

---

### Task 1: POST executor (`writer.py`)

**Files:**
- Create: `src/connectwise_mcp/writer.py`
- Test: `tests/test_writer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_writer.py`:

```python
"""Writer (POST executor) tests against a mock ConnectWise (no network)."""

import httpx
import pytest

from connectwise_mcp.executor import ExecutionError
from connectwise_mcp.writer import ALLOWED_POSTS, cw_post


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


async def test_allowlist_blocks_unknown_paths_before_any_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(201, json={"id": 1})

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="not allowed"):
            await cw_post(client, "/service/tickets/{id}", {"summary": "x"})
        with pytest.raises(ExecutionError, match="not allowed"):
            await cw_post(client, "/system/members", {})
    assert calls["n"] == 0


async def test_all_five_allowed_paths_post_json_body():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(201, json={"id": 1})

    async with make_client(handler) as client:
        for path, params in [
            ("/service/tickets", None),
            ("/time/entries", None),
            ("/service/tickets/{parentId}/notes", {"parentId": 7}),
            ("/company/companies", None),
            ("/company/contacts", None),
        ]:
            out = await cw_post(client, path, {"a": 1}, path_params=params)
            assert out == {"id": 1}
    assert ("POST", "/api/service/tickets/7/notes") in seen
    assert len(seen) == 5


async def test_missing_path_param_raises():
    async with make_client(lambda r: httpx.Response(201, json={})) as client:
        with pytest.raises(ExecutionError, match="parentId"):
            await cw_post(client, "/service/tickets/{parentId}/notes", {"text": "x"})


async def test_created_record_info_stripped():
    def handler(request):
        return httpx.Response(
            201, json={"id": 9, "_info": {"u": "x"}, "company": {"id": 1, "_info": {}}}
        )

    async with make_client(handler) as client:
        out = await cw_post(client, "/service/tickets", {"summary": "s"})
    assert out == {"id": 9, "company": {"id": 1}}


async def test_400_surfaces_validation_detail():
    def handler(request):
        return httpx.Response(400, text='{"errors":[{"message":"identifier taken"}]}')

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="identifier taken"):
            await cw_post(client, "/company/companies", {"name": "X"})


async def test_retry_on_429_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(201, json={"id": 2})

    async with make_client(handler) as client:
        out = await cw_post(client, "/time/entries", {"actualHours": 1})
    assert calls["n"] == 2
    assert out == {"id": 2}


async def test_no_retry_on_500():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="500"):
            await cw_post(client, "/service/tickets", {"summary": "s"})
    assert calls["n"] == 1


async def test_retry_on_connect_error_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(201, json={"id": 3})

    async with make_client(handler) as client:
        out = await cw_post(client, "/service/tickets", {"summary": "s"})
    assert calls["n"] == 2
    assert out == {"id": 3}


async def test_no_retry_on_read_timeout():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="may or may not have been created"):
            await cw_post(client, "/service/tickets", {"summary": "s"})
    assert calls["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_writer.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'connectwise_mcp.writer'`

- [ ] **Step 3: Implement `src/connectwise_mcp/writer.py`**

```python
"""Executes the five allowlisted POST calls against ConnectWise.

This module is the only write path in the package. The allowlist below is the
write-scope guarantee: any path not listed here cannot be POSTed, by
construction. Retry semantics deliberately differ from the GET executor —
POSTs are not idempotent, so we only retry when the request provably never
reached ConnectWise (connection failures) or when ConnectWise explicitly asks
(429). Never on 5xx or read timeouts, which could double-create records.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .executor import ExecutionError, _classify_error, _fill_path, strip_info

ALLOWED_POSTS = frozenset(
    {
        "/service/tickets",
        "/time/entries",
        "/service/tickets/{parentId}/notes",
        "/company/companies",
        "/company/contacts",
    }
)

_MAX_RETRIES = 3


def _classify_post_error(status: int, body: str) -> str:
    if status == 400:
        return (
            "400 Bad Request: ConnectWise rejected the new record "
            f"(validation). Detail: {body[:800]}"
        )
    return _classify_error(status, body)


async def _post_with_retries(
    client: httpx.AsyncClient, url: str, body: dict[str, Any]
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.post(url, json=body)
        except httpx.ConnectError as e:
            # Connection never established: the request did not reach CW,
            # so retrying cannot double-create.
            last_exc = e
            if attempt == _MAX_RETRIES:
                break
            await asyncio.sleep(2**attempt)
            continue
        except httpx.TransportError as e:
            # Sent (or possibly sent) but no response: the record may or may
            # not exist. Do NOT retry; tell the caller to check first.
            raise ExecutionError(
                f"Network error after sending POST {url}: {e}. The record may "
                "or may not have been created — check with a read tool before "
                "retrying."
            ) from e
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            await asyncio.sleep(min(delay, 30.0))
            continue
        return resp
    raise ExecutionError(f"Could not connect to ConnectWise: {last_exc}")


async def cw_post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    path_params: dict[str, Any] | None = None,
) -> Any:
    """POST ``body`` to one of the allowlisted paths; return the created record."""
    if path not in ALLOWED_POSTS:
        raise ExecutionError(
            f"POST {path!r} is not allowed. This server only writes to: "
            + ", ".join(sorted(ALLOWED_POSTS))
        )
    url = _fill_path(path, path_params)
    resp = await _post_with_retries(client, url, body)
    if resp.status_code >= 400:
        raise ExecutionError(_classify_post_error(resp.status_code, resp.text))
    try:
        data = resp.json()
    except ValueError:
        return {"raw": resp.text}
    return strip_info(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_writer.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/connectwise_mcp/writer.py tests/test_writer.py
git commit -m "feat: POST executor with five-path allowlist and POST-safe retries"
```

---

### Task 2: Resolution helpers (`curated_writes.py`, part 1)

**Files:**
- Create: `src/connectwise_mcp/curated_writes.py`
- Test: `tests/test_write_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_write_tools.py`:

```python
"""Write-tool tests: resolution helpers and body builders (no network)."""

import json

import httpx
import pytest

from connectwise_mcp.curated_writes import (
    ResolutionError,
    _resolve_board,
    _resolve_company,
    _resolve_contact,
    _resolve_member,
)


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


def json_response(items):
    return httpx.Response(200, json=items)


async def test_resolve_company_exact_identifier_wins():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier = 'SafePointIT'" in cond:
            return json_response([{"id": 19332, "identifier": "SafePointIT", "name": "SafePoint IT"}])
        raise AssertionError(f"unexpected conditions: {cond}")

    async with make_client(handler) as client:
        assert await _resolve_company(client, "SafePointIT") == 19332


async def test_resolve_company_falls_back_to_name_contains():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier =" in cond:
            return json_response([])
        assert "name contains 'SafePoint'" in cond
        return json_response([{"id": 19332, "identifier": "SafePointIT", "name": "SafePoint IT"}])

    async with make_client(handler) as client:
        assert await _resolve_company(client, "SafePoint") == 19332


async def test_resolve_company_ambiguous_lists_candidates():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier =" in cond:
            return json_response([])
        return json_response(
            [
                {"id": 1, "identifier": "ACME", "name": "Acme Inc"},
                {"id": 2, "identifier": "ACMEUK", "name": "Acme UK"},
            ]
        )

    async with make_client(handler) as client:
        with pytest.raises(ResolutionError) as exc:
            await _resolve_company(client, "Acme")
    assert "ACMEUK" in str(exc.value)


async def test_resolve_company_no_match():
    def handler(request):
        return json_response([])

    async with make_client(handler) as client:
        with pytest.raises(ResolutionError, match="No company matched"):
            await _resolve_company(client, "Nope")


async def test_resolve_board_found_and_not_found():
    def handler(request):
        cond = request.url.params.get("conditions", "")
        if "name = 'Service Desk'" in cond:
            return json_response([{"id": 27, "name": "Service Desk"}])
        if "name = 'Wrong'" in cond:
            return json_response([])
        # the fallback "list all boards" request has no conditions
        return json_response([{"id": 27, "name": "Service Desk"}, {"id": 28, "name": "Projects"}])

    async with make_client(handler) as client:
        assert await _resolve_board(client, "Service Desk") == 27
        with pytest.raises(ResolutionError, match="Projects"):
            await _resolve_board(client, "Wrong")


async def test_resolve_member():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier = 'wyoung'" in cond:
            return json_response([{"id": 150, "identifier": "wyoung"}])
        return json_response([])

    async with make_client(handler) as client:
        assert await _resolve_member(client, "wyoung") == 150
        with pytest.raises(ResolutionError, match="No member"):
            await _resolve_member(client, "ghost")


async def test_resolve_contact_two_part_name_scoped_to_company():
    def handler(request):
        cond = request.url.params["conditions"]
        assert "company/id = 19332" in cond
        assert "firstName contains 'Jane'" in cond
        assert "lastName contains 'Doe'" in cond
        return json_response([{"id": 88, "firstName": "Jane", "lastName": "Doe"}])

    async with make_client(handler) as client:
        assert await _resolve_contact(client, "Jane Doe", 19332) == 88


async def test_resolve_contact_single_token_matches_either_name():
    def handler(request):
        cond = request.url.params["conditions"]
        assert "firstName contains 'Jane' or lastName contains 'Jane'" in cond
        return json_response([{"id": 88, "firstName": "Jane", "lastName": "Doe"}])

    async with make_client(handler) as client:
        assert await _resolve_contact(client, "Jane", 19332) == 88
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_write_tools.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'connectwise_mcp.curated_writes'`

- [ ] **Step 3: Implement the helpers**

Create `src/connectwise_mcp/curated_writes.py`:

```python
"""Curated write tools: the five create actions this server allows.

Each tool resolves friendly inputs (company name/identifier, board name,
member identifier, contact name) to ConnectWise IDs via the read executor,
builds a minimal POST body, and writes through writer.cw_post — the only
write path in the package. Tools return the created record (with its id) so
the model can confirm and chain follow-ups.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import MissingCredentials, get_credentials
from .catalog import load_catalog
from .client import make_client
from .conditions import join_and, quote
from .executor import ExecutionError, cw_get
from .writer import cw_post


class ResolutionError(ExecutionError):
    """A friendly input could not be resolved to exactly one record."""


async def _run_write(fn) -> Any:
    """Resolve credentials, open a client, run ``fn(client)``, map errors."""
    try:
        creds = get_credentials()
    except MissingCredentials as e:
        return {"error": str(e)}
    try:
        async with make_client(creds) as client:
            return await fn(client)
    except ExecutionError as e:
        return {"error": str(e)}


async def _get_items(client, path: str, **kwargs: Any) -> list[dict]:
    out = await cw_get(client, load_catalog(), path, **kwargs)
    if isinstance(out, dict):
        return out.get("items") or []
    return []


async def _resolve_company(client, query: str) -> int:
    """Company by exact identifier, else name-contains. One match or error."""
    items = await _get_items(
        client,
        "/company/companies",
        conditions=join_and(f"identifier = {quote(query)}", "deletedFlag = false"),
        fields="id,identifier,name",
        page_size=5,
    )
    if not items:
        items = await _get_items(
            client,
            "/company/companies",
            conditions=join_and(
                f"name contains {quote(query)}", "deletedFlag = false"
            ),
            fields="id,identifier,name",
            page_size=5,
        )
    if not items:
        raise ResolutionError(
            f"No company matched {query!r}. Use find_company to locate it."
        )
    if len(items) > 1:
        cands = "; ".join(
            f"id={i['id']} {i['identifier']} ({i['name']})" for i in items
        )
        raise ResolutionError(
            f"Ambiguous company {query!r} — candidates: {cands}. "
            "Pass the exact identifier."
        )
    return items[0]["id"]


async def _resolve_board(client, name: str) -> int:
    items = await _get_items(
        client,
        "/service/boards",
        conditions=f"name = {quote(name)}",
        fields="id,name",
        page_size=5,
    )
    if len(items) == 1:
        return items[0]["id"]
    boards = await _get_items(
        client, "/service/boards", fields="id,name", page_size=100
    )
    names = ", ".join(b["name"] for b in boards)
    raise ResolutionError(f"Board {name!r} not found. Available boards: {names}")


async def _resolve_member(client, identifier: str) -> int:
    items = await _get_items(
        client,
        "/system/members",
        conditions=f"identifier = {quote(identifier)}",
        fields="id,identifier",
        page_size=5,
    )
    if len(items) == 1:
        return items[0]["id"]
    raise ResolutionError(
        f"No member with identifier {quote(identifier)}. Omit `member` to use "
        "the API member, or check the identifier."
    )


async def _resolve_contact(client, name: str, company_id: int) -> int:
    parts = name.split()
    if len(parts) >= 2:
        name_cond = (
            f"firstName contains {quote(parts[0])} and "
            f"lastName contains {quote(parts[-1])}"
        )
    else:
        name_cond = (
            f"(firstName contains {quote(name)} or "
            f"lastName contains {quote(name)})"
        )
    items = await _get_items(
        client,
        "/company/contacts",
        conditions=join_and(name_cond, f"company/id = {company_id}"),
        fields="id,firstName,lastName",
        page_size=5,
    )
    if not items:
        raise ResolutionError(
            f"No contact matching {name!r} at company id {company_id}."
        )
    if len(items) > 1:
        cands = "; ".join(
            f"id={i['id']} {i.get('firstName', '')} {i.get('lastName', '')}".strip()
            for i in items
        )
        raise ResolutionError(
            f"Ambiguous contact {name!r} — candidates: {cands}."
        )
    return items[0]["id"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_write_tools.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/connectwise_mcp/curated_writes.py tests/test_write_tools.py
git commit -m "feat: friendly-name resolution helpers for write tools"
```

---

### Task 3: Body builders (`curated_writes.py`, part 2)

**Files:**
- Modify: `src/connectwise_mcp/curated_writes.py` (append after `_resolve_contact`)
- Test: `tests/test_write_tools.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_write_tools.py`:

```python
from connectwise_mcp.curated_writes import (  # noqa: E402
    _company_body,
    _contact_body,
    _default_time_start,
    _note_body,
    _ticket_body,
    _time_entry_body,
)


def test_ticket_body_minimal_and_full():
    assert _ticket_body("Printer down", 19332, 27) == {
        "summary": "Printer down",
        "company": {"id": 19332},
        "board": {"id": 27},
    }
    full = _ticket_body(
        "Printer down",
        19332,
        27,
        initial_description="It smokes",
        priority="Priority 1 - Critical",
        status="New",
        contact_id=88,
    )
    assert full["initialDescription"] == "It smokes"
    assert full["priority"] == {"name": "Priority 1 - Critical"}
    assert full["status"] == {"name": "New"}
    assert full["contact"] == {"id": 88}


def test_time_entry_body_ticket_vs_company():
    t = _time_entry_body(
        hours=0.5,
        time_start="2026-06-10T15:00:00Z",
        ticket_id=132,
        company_id=None,
        member_id=None,
        notes="did things",
        billable=True,
    )
    assert t == {
        "timeStart": "2026-06-10T15:00:00Z",
        "actualHours": 0.5,
        "chargeToId": 132,
        "chargeToType": "ServiceTicket",
        "notes": "did things",
        "billableOption": "Billable",
    }
    c = _time_entry_body(
        hours=2,
        time_start="2026-06-10T15:00:00Z",
        ticket_id=None,
        company_id=19332,
        member_id=150,
        notes=None,
        billable=False,
    )
    assert c["company"] == {"id": 19332}
    assert c["chargeToType"] == "Company"
    assert c["member"] == {"id": 150}
    assert c["billableOption"] == "DoNotBill"
    assert "chargeToId" not in c
    assert "notes" not in c


def test_default_time_start_subtracts_hours():
    ts = _default_time_start(2.0, now=datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc))
    assert ts == "2026-06-10T13:00:00Z"


def test_note_body_flag_mapping():
    assert _note_body("hello", "discussion") == {
        "text": "hello",
        "detailDescriptionFlag": True,
    }
    assert _note_body("hush", "internal") == {
        "text": "hush",
        "internalAnalysisFlag": True,
    }
    assert _note_body("fixed", "resolution") == {
        "text": "fixed",
        "resolutionFlag": True,
    }
    with pytest.raises(ValueError, match="note_type"):
        _note_body("x", "shouting")


def test_company_body():
    assert _company_body("Acme Inc", "ACME") == {"name": "Acme Inc", "identifier": "ACME"}
    full = _company_body(
        "Acme Inc",
        "ACME",
        phone="555-0100",
        website="https://acme.test",
        address_line="1 Main St",
        city="Springfield",
        state="IL",
        zip_code="62701",
        company_type="Prospect",
        status="Active",
    )
    assert full["phoneNumber"] == "555-0100"
    assert full["website"] == "https://acme.test"
    assert full["addressLine1"] == "1 Main St"
    assert full["city"] == "Springfield"
    assert full["state"] == "IL"
    assert full["zip"] == "62701"
    assert full["types"] == [{"name": "Prospect"}]
    assert full["status"] == {"name": "Active"}


def test_contact_body():
    assert _contact_body("Jane") == {"firstName": "Jane"}
    full = _contact_body(
        "Jane",
        last_name="Doe",
        company_id=19332,
        email="jane@acme.test",
        phone="555-0101",
        title="CTO",
    )
    assert full["lastName"] == "Doe"
    assert full["company"] == {"id": 19332}
    assert full["title"] == "CTO"
    assert full["communicationItems"] == [
        {
            "type": {"name": "Email"},
            "value": "jane@acme.test",
            "defaultFlag": True,
            "communicationType": "Email",
        },
        {
            "type": {"name": "Phone"},
            "value": "555-0101",
            "defaultFlag": True,
            "communicationType": "Phone",
        },
    ]
```

Also add to the existing imports at the top of the test file:

```python
from datetime import datetime, timezone
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_write_tools.py -v`
Expected: ImportError — `cannot import name '_company_body'`

- [ ] **Step 3: Implement the body builders**

Append to `src/connectwise_mcp/curated_writes.py`:

```python
# ------------------------------------------------------------- body builders

_NOTE_FLAGS = {
    "discussion": "detailDescriptionFlag",
    "internal": "internalAnalysisFlag",
    "resolution": "resolutionFlag",
}


def _ticket_body(
    summary: str,
    company_id: int,
    board_id: int,
    initial_description: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    contact_id: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": summary,
        "company": {"id": company_id},
        "board": {"id": board_id},
    }
    if initial_description:
        body["initialDescription"] = initial_description
    if priority:
        body["priority"] = {"name": priority}
    if status:
        body["status"] = {"name": status}
    if contact_id is not None:
        body["contact"] = {"id": contact_id}
    return body


def _default_time_start(hours: float, now: datetime | None = None) -> str:
    dt = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_entry_body(
    *,
    hours: float,
    time_start: str,
    ticket_id: int | None,
    company_id: int | None,
    member_id: int | None,
    notes: str | None,
    billable: bool | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"timeStart": time_start, "actualHours": hours}
    if ticket_id is not None:
        body["chargeToId"] = ticket_id
        body["chargeToType"] = "ServiceTicket"
    else:
        body["company"] = {"id": company_id}
        body["chargeToType"] = "Company"
    if member_id is not None:
        body["member"] = {"id": member_id}
    if notes:
        body["notes"] = notes
    if billable is not None:
        body["billableOption"] = "Billable" if billable else "DoNotBill"
    return body


def _note_body(text: str, note_type: str) -> dict[str, Any]:
    flag = _NOTE_FLAGS.get(note_type)
    if flag is None:
        raise ValueError(
            f"note_type must be one of {sorted(_NOTE_FLAGS)}, got {note_type!r}"
        )
    return {"text": text, flag: True}


def _company_body(
    name: str,
    identifier: str,
    *,
    phone: str | None = None,
    website: str | None = None,
    address_line: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    company_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "identifier": identifier}
    if phone:
        body["phoneNumber"] = phone
    if website:
        body["website"] = website
    if address_line:
        body["addressLine1"] = address_line
    if city:
        body["city"] = city
    if state:
        body["state"] = state
    if zip_code:
        body["zip"] = zip_code
    if company_type:
        body["types"] = [{"name": company_type}]
    if status:
        body["status"] = {"name": status}
    return body


def _contact_body(
    first_name: str,
    *,
    last_name: str | None = None,
    company_id: int | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"firstName": first_name}
    if last_name:
        body["lastName"] = last_name
    if company_id is not None:
        body["company"] = {"id": company_id}
    if title:
        body["title"] = title
    comm: list[dict[str, Any]] = []
    if email:
        comm.append(
            {
                "type": {"name": "Email"},
                "value": email,
                "defaultFlag": True,
                "communicationType": "Email",
            }
        )
    if phone:
        comm.append(
            {
                "type": {"name": "Phone"},
                "value": phone,
                "defaultFlag": True,
                "communicationType": "Phone",
            }
        )
    if comm:
        body["communicationItems"] = comm
    return body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_write_tools.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/connectwise_mcp/curated_writes.py tests/test_write_tools.py
git commit -m "feat: POST body builders for the five create actions"
```

---

### Task 4: Register the five tools (`curated_writes.py`, part 3 + `server.py`)

**Files:**
- Modify: `src/connectwise_mcp/curated_writes.py` (append `register`)
- Modify: `src/connectwise_mcp/server.py` (import + register + instructions)
- Test: `tests/test_tools.py` (extend `EXPECTED_TOOLS`), `tests/test_write_tools.py` (append)

- [ ] **Step 1: Write the failing tests**

In `tests/test_tools.py`, replace the `EXPECTED_TOOLS` set with:

```python
EXPECTED_TOOLS = {
    "list_modules",
    "search_endpoints",
    "describe_endpoint",
    "cw_get",
    "search_tickets",
    "get_ticket",
    "find_company",
    "list_agreements",
    "recent_time_entries",
    "create_ticket",
    "create_time_entry",
    "create_ticket_note",
    "create_company",
    "create_contact",
}
```

Append to `tests/test_write_tools.py` (end-to-end through the MCP server with mocked credentials/transport):

```python
import connectwise_mcp.curated_writes as cw_writes  # noqa: E402
from connectwise_mcp.auth import CWCredentials  # noqa: E402


@pytest.fixture
def served(monkeypatch):
    """Patch credentials + transport; capture POSTs. Returns the capture list."""
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(
                (request.url.path, json.loads(request.content.decode()))
            )
            return httpx.Response(201, json={"id": 999, "_info": {"x": 1}})
        # resolution GETs
        path = request.url.path
        if path.endswith("/company/companies"):
            return httpx.Response(
                200,
                json=[{"id": 19332, "identifier": "SafePointIT", "name": "SafePoint IT"}],
            )
        if path.endswith("/service/boards"):
            return httpx.Response(200, json=[{"id": 27, "name": "Service Desk"}])
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        cw_writes,
        "get_credentials",
        lambda: CWCredentials("co", "pub", "priv", "cid"),
    )
    monkeypatch.setattr(
        cw_writes,
        "make_client",
        lambda creds: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
        ),
    )
    return posted


async def test_create_ticket_tool_resolves_and_posts(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_ticket",
            {"summary": "Printer down", "company": "SafePointIT", "board": "Service Desk"},
        )
    assert len(served) == 1
    path, body = served[0]
    assert path == "/api/service/tickets"
    assert body == {
        "summary": "Printer down",
        "company": {"id": 19332},
        "board": {"id": 27},
    }
    assert result.data["id"] == 999
    assert "_info" not in result.data


async def test_create_time_entry_requires_exactly_one_target(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        both = await client.call_tool(
            "create_time_entry",
            {"hours": 1, "ticket_id": 132, "company": "SafePointIT"},
        )
        neither = await client.call_tool("create_time_entry", {"hours": 1})
    assert "exactly one" in both.data["error"]
    assert "exactly one" in neither.data["error"]
    assert served == []  # nothing was posted


async def test_create_ticket_note_posts_to_parent(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        await client.call_tool(
            "create_ticket_note", {"ticket_id": 132, "text": "looked at it"}
        )
    path, body = served[0]
    assert path == "/api/service/tickets/132/notes"
    assert body == {"text": "looked at it", "detailDescriptionFlag": True}


async def test_create_company_identifier_length_check(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        out = await client.call_tool(
            "create_company",
            {"name": "X", "identifier": "THIS_IDENTIFIER_IS_WAY_TOO_LONG"},
        )
    assert "25" in out.data["error"]
    assert served == []


async def test_create_contact_resolves_company(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        await client.call_tool(
            "create_contact",
            {"first_name": "Jane", "last_name": "Doe", "company": "SafePointIT",
             "email": "jane@safepoint.test"},
        )
    path, body = served[0]
    assert path == "/api/company/contacts"
    assert body["company"] == {"id": 19332}
    assert body["communicationItems"][0]["value"] == "jane@safepoint.test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_write_tools.py tests/test_tools.py -v`
Expected: the new tests FAIL (`ImportError`/unknown tool `create_ticket`); pre-existing tests still pass.

- [ ] **Step 3: Implement `register` and wire into the server**

Append to `src/connectwise_mcp/curated_writes.py`:

```python
# ------------------------------------------------------------------- tools


def register(mcp) -> None:
    @mcp.tool
    async def create_ticket(
        summary: str,
        company: str,
        board: str,
        initial_description: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        contact: str | None = None,
    ) -> Any:
        """Create a service ticket. WRITES to ConnectWise.

        - company: company name or identifier (resolved automatically)
        - board: service board name, e.g. "Service Desk"
        - priority/status: names as shown in ConnectWise (optional)
        - contact: contact name at that company (optional)
        Returns the created ticket (including its id).
        """

        async def go(client):
            company_id = await _resolve_company(client, company)
            board_id = await _resolve_board(client, board)
            contact_id = (
                await _resolve_contact(client, contact, company_id)
                if contact
                else None
            )
            body = _ticket_body(
                summary,
                company_id,
                board_id,
                initial_description=initial_description,
                priority=priority,
                status=status,
                contact_id=contact_id,
            )
            return await cw_post(client, "/service/tickets", body)

        return await _run_write(go)

    @mcp.tool
    async def create_time_entry(
        hours: float,
        ticket_id: int | None = None,
        company: str | None = None,
        notes: str | None = None,
        time_start: str | None = None,
        member: str | None = None,
        billable: bool | None = None,
    ) -> Any:
        """Create a time entry. WRITES to ConnectWise.

        Charge target: pass exactly one of ticket_id (service ticket) or
        company (name/identifier). time_start is ISO UTC
        (YYYY-MM-DDTHH:MM:SSZ); default is now minus `hours`. member is a
        member identifier; default is the API member. billable maps to
        Billable/DoNotBill; omit for the work-type default.
        """
        if (ticket_id is None) == (company is None):
            return {
                "error": "Pass exactly one of ticket_id or company as the "
                "charge target."
            }

        async def go(client):
            company_id = (
                await _resolve_company(client, company) if company else None
            )
            member_id = await _resolve_member(client, member) if member else None
            body = _time_entry_body(
                hours=hours,
                time_start=time_start or _default_time_start(hours),
                ticket_id=ticket_id,
                company_id=company_id,
                member_id=member_id,
                notes=notes,
                billable=billable,
            )
            return await cw_post(client, "/time/entries", body)

        return await _run_write(go)

    @mcp.tool
    async def create_ticket_note(
        ticket_id: int,
        text: str,
        note_type: str = "discussion",
    ) -> Any:
        """Add a note to a service ticket. WRITES to ConnectWise.

        note_type: "discussion" (customer-facing, default), "internal"
        (internal analysis), or "resolution".
        """
        try:
            body = _note_body(text, note_type)
        except ValueError as e:
            return {"error": str(e)}

        async def go(client):
            return await cw_post(
                client,
                "/service/tickets/{parentId}/notes",
                body,
                path_params={"parentId": ticket_id},
            )

        return await _run_write(go)

    @mcp.tool
    async def create_company(
        name: str,
        identifier: str,
        phone: str | None = None,
        website: str | None = None,
        address_line: str | None = None,
        city: str | None = None,
        state: str | None = None,
        zip_code: str | None = None,
        company_type: str | None = None,
        status: str | None = None,
    ) -> Any:
        """Create a company. WRITES to ConnectWise.

        identifier must be unique and at most 25 characters (CW limit).
        company_type/status are names as configured in ConnectWise.
        """
        if len(identifier) > 25:
            return {
                "error": f"identifier {identifier!r} is "
                f"{len(identifier)} chars; ConnectWise allows at most 25."
            }

        async def go(client):
            body = _company_body(
                name,
                identifier,
                phone=phone,
                website=website,
                address_line=address_line,
                city=city,
                state=state,
                zip_code=zip_code,
                company_type=company_type,
                status=status,
            )
            return await cw_post(client, "/company/companies", body)

        return await _run_write(go)

    @mcp.tool
    async def create_contact(
        first_name: str,
        last_name: str | None = None,
        company: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        title: str | None = None,
    ) -> Any:
        """Create a contact, optionally attached to a company. WRITES to
        ConnectWise.

        company is a name or identifier (resolved automatically). email and
        phone become the contact's default communication items.
        """

        async def go(client):
            company_id = (
                await _resolve_company(client, company) if company else None
            )
            body = _contact_body(
                first_name,
                last_name=last_name,
                company_id=company_id,
                email=email,
                phone=phone,
                title=title,
            )
            return await cw_post(client, "/company/contacts", body)

        return await _run_write(go)
```

In `src/connectwise_mcp/server.py`, change the import line:

```python
from . import config, curated
```

to:

```python
from . import config, curated, curated_writes
```

change the `instructions=` string in the `FastMCP(...)` call to:

```python
    instructions=(
        "Access to ConnectWise Manage (PSA). Reads: use the curated tools "
        "(search_tickets, get_ticket, find_company, list_agreements, "
        "recent_time_entries) for common asks; for anything else use "
        "search_endpoints to find the GET endpoint, optionally "
        "describe_endpoint for its parameters, then cw_get to fetch data "
        "(see cw_get docs for `conditions` filter syntax). Writes: exactly "
        "five create actions are available — create_ticket, "
        "create_time_entry, create_ticket_note, create_company, "
        "create_contact. Everything else is read-only."
    ),
```

and change the registration line at the bottom:

```python
curated.register(mcp)
```

to:

```python
curated.register(mcp)
curated_writes.register(mcp)
```

Also update the module docstring's tool list in `server.py` (the
"plus a handful of curated tools" sentence) to:

```python
plus curated read tools (search_tickets, find_company, ...) and five curated
write tools (create_ticket, create_time_entry, create_ticket_note,
create_company, create_contact) — see curated.py / curated_writes.py.
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: all tests pass (existing 17 + 9 writer + ~21 write-tool tests).

- [ ] **Step 5: Commit**

```bash
git add src/connectwise_mcp/curated_writes.py src/connectwise_mcp/server.py tests/test_tools.py tests/test_write_tools.py
git commit -m "feat: register five create tools on the MCP server"
```

---

### Task 5: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the README**

1. Change the intro line `as a read-only gateway for AI agents.` to
   `as a gateway for AI agents: catalog-wide reads plus five curated create actions.`
2. In the tool table, add after the `recent_time_entries` row:

```markdown
| `create_ticket` / `create_ticket_note` | Create a service ticket / add a note to one |
| `create_time_entry` | Log time against a ticket or company |
| `create_company` / `create_contact` | Create a company / contact |
```

3. Replace the line
   `**Read-only by construction:** there is no create/update/delete code path.`
   with:

```markdown
**Write scope by construction:** reads cover the whole GET catalog; writes
are limited to five allowlisted POST paths in `writer.py` (ticket, time
entry, ticket note, company, contact). There is no update/delete code path,
and no generic write gateway.
```

- [ ] **Step 2: Run the full suite one more time**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the five write actions and the new scope guarantee"
```

---

### Task 6: Live verification (real instance — creates real records)

**Files:** none (manual verification, run from repo root with CW_* env vars set; read them from the Claude Desktop config's connectwise entry, not from disk notes)

- [ ] **Step 1: Restart check** — confirm the installed package picks up the new tools:

Run: `.venv\Scripts\python.exe -c "from connectwise_mcp.server import mcp; import asyncio; print(sorted(t.name for t in asyncio.run(mcp.list_tools())))"`
Expected: list includes the five create_* tools.

- [ ] **Step 2: Create labeled test records, in dependency order**, via a short script using the fastmcp in-memory client (pattern as in tests, but env credentials, no mock transport): create_company (name "ZZZ MCP Write Test", identifier "ZZZMCPTEST") → create_contact ("Test Contact", company "ZZZMCPTEST") → create_ticket (summary "MCP write test — safe to delete", company "ZZZMCPTEST", board "Service Desk") → create_ticket_note (that ticket id, "MCP write test note") → create_time_entry (that ticket id, 0.25 h, notes "MCP write test").

- [ ] **Step 3: Report** all five created IDs to the user for manual cleanup in ConnectWise (deletes are out of scope by design). If any call 400s, surface the CW validation message, fix the body builder, add a regression test, and re-run.
