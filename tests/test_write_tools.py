"""Write-tool tests: resolution helpers and body builders (no network)."""

import json
from datetime import datetime, timezone

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


async def test_create_time_entry_happy_path_defaults_time_start(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_time_entry",
            {"hours": 0.25, "ticket_id": 132, "notes": "quick fix"},
        )
    path, body = served[0]
    assert path == "/api/time/entries"
    assert body["chargeToId"] == 132
    assert body["chargeToType"] == "ServiceTicket"
    assert body["actualHours"] == 0.25
    assert body["notes"] == "quick fix"
    # default time_start was computed and formatted as CW ISO-Z
    assert body["timeStart"].endswith("Z") and "T" in body["timeStart"]
    assert result.data["id"] == 999


async def test_create_company_happy_path(served):
    from fastmcp import Client
    from connectwise_mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_company",
            {"name": "Acme Inc", "identifier": "ACME", "city": "Springfield"},
        )
    path, body = served[0]
    assert path == "/api/company/companies"
    assert body == {"name": "Acme Inc", "identifier": "ACME", "city": "Springfield"}
    assert result.data["id"] == 999
