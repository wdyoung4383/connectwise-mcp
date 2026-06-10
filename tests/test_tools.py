"""Server/tool-level tests: registration, descriptions, conditions, schema caps."""

import pytest

from connectwise_mcp.catalog import load_catalog
from connectwise_mcp.conditions import eq, join_and, quote
from connectwise_mcp.server import mcp

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


async def test_all_tools_registered_with_descriptions():
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert EXPECTED_TOOLS <= set(by_name)
    for name in EXPECTED_TOOLS:
        assert by_name[name].description, f"{name} has no description"
    # the conditions cheatsheet must reach the model via cw_get
    assert "conditions" in by_name["cw_get"].description
    assert "single-quoted" in by_name["cw_get"].description


def test_quote_variants():
    assert quote(5) == "5"
    assert quote(True) == "true"
    assert quote("O'Brien") == "'O\\'Brien'"
    assert eq("status/name", "Open") == "status/name = 'Open'"
    assert join_and("a", "", "b") == "a and b"


def test_describe_schema_capped():
    cat = load_catalog()
    d = cat.describe("getServiceTicketsById")
    schema = d["response_schema"]
    assert schema["type"] == "object"
    assert len(schema["properties"]) <= cat.MAX_PROPS
    assert "more fields omitted" in schema.get("note", "")


def test_curated_paths_in_scope():
    cat = load_catalog()
    for path in (
        "/service/tickets",
        "/service/tickets/{id}",
        "/service/tickets/{parentId}/notes",
        "/company/companies",
        "/finance/agreements",
        "/time/entries",
    ):
        assert cat.by_path(path), f"{path} missing from catalog"


async def test_health_route():
    import httpx

    from connectwise_mcp.server import mcp

    app = mcp.http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
