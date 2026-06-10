"""FastMCP server exposing ConnectWise PSA as a gateway (reads + five creates).

Design: rather than emit one tool per endpoint (324 GETs in scope), we expose a
small set of gateway tools over a runtime catalog of the OpenAPI spec:

    list_modules        -> orientation
    search_endpoints    -> find the right GET endpoint
    describe_endpoint   -> see its exact params + response shape
    cw_get              -> execute any in-scope GET (with CW paging/conditions)

plus curated read tools (search_tickets, find_company, ...) and five curated
write tools (create_ticket, create_time_entry, create_ticket_note,
create_company, create_contact) — see curated.py / curated_writes.py.

Credentials are per request: Authorization Bearer token -> tenant store
(hosted), X-CW-* headers (custom agents), or CW_* env vars (local stdio).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import config, curated, curated_writes
from .catalog import load_catalog
from .conditions import CONDITIONS_HELP
from .curated import run_get

mcp = FastMCP(
    name="connectwise-psa",
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
)

catalog = load_catalog()


@mcp.tool
def list_modules() -> dict[str, Any]:
    """List the ConnectWise modules in scope and how many GET endpoints each has.

    Use this for orientation before searching (e.g. service, company, finance,
    project, sales, time, schedule, procurement, system).
    """
    return {"modules": catalog.modules(), "total_endpoints": len(catalog.endpoints)}


@mcp.tool
def search_endpoints(
    query: str, module: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Search the in-scope read endpoints by keyword.

    Matches operationId, path, summary and category/tag. Optionally restrict to
    one `module` (see list_modules). Returns enough detail (path + params) to
    often skip describe_endpoint.

    Example: search_endpoints("open tickets", module="service")
    """
    results = catalog.search(query, module=module, limit=limit)
    return [
        {
            "operationId": ep.operation_id,
            "path": ep.path,
            "module": ep.module,
            "tags": ep.tags,
            "summary": ep.summary,
            "path_params": ep.path_params,
        }
        for ep in results
    ]


@mcp.tool
def describe_endpoint(operation_id: str) -> dict[str, Any]:
    """Return the full contract for one endpoint: parameters and response shape.

    Pass the `operationId` from search_endpoints. Use this when you need the
    exact query params or the structure of the returned object.
    """
    desc = catalog.describe(operation_id)
    if desc is None:
        return {"error": f"Unknown operationId {operation_id!r}. Try search_endpoints."}
    return desc


# Note: built with `+`, so it must be passed explicitly — a concatenation
# expression at the top of a function body is not a real docstring.
_CW_GET_DOC = (
    """Execute an in-scope ConnectWise GET and return the JSON result.

    `path` is a path from search_endpoints, e.g. "/service/tickets" or
    "/service/tickets/{id}". Fill `{...}` segments via `path_params`
    (e.g. {"id": 123}).

    List responses come back as {"items", "count", "has_more", ...}; request
    the next `page` while has_more is true. `fields` projects a subset of
    columns (comma-separated) to slim responses. `order_by` sorts (e.g.
    "id desc"). Verbose `_info` metadata is stripped unless include_info=true.

    Filtering uses the ConnectWise `conditions` query language:
    """
    + CONDITIONS_HELP
)


@mcp.tool(description=_CW_GET_DOC)
async def cw_get(
    path: str,
    path_params: dict[str, Any] | None = None,
    conditions: str | None = None,
    child_conditions: str | None = None,
    order_by: str | None = None,
    fields: str | None = None,
    page: int | None = None,
    page_size: int = config.DEFAULT_PAGE_SIZE,
    page_id: int | None = None,
    include_info: bool = False,
) -> Any:
    return await run_get(
        path,
        path_params=path_params,
        conditions=conditions,
        child_conditions=child_conditions,
        order_by=order_by,
        fields=fields,
        page=page,
        page_size=page_size,
        page_id=page_id,
        include_info=include_info,
    )


curated.register(mcp)
curated_writes.register(mcp)


def main() -> None:
    """Run the server. Defaults to HTTP; set CW_MCP_TRANSPORT=stdio for local."""
    import os

    transport = os.getenv("CW_MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host=config.HTTP_HOST, port=config.HTTP_PORT)


if __name__ == "__main__":
    main()
