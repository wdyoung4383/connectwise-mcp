"""FastMCP server exposing ConnectWise PSA as a full-surface gateway.

Design: rather than emit one tool per operation (2,780 in the catalog), we
expose a small set of gateway tools over a runtime catalog of the OpenAPI spec:

    list_modules        -> orientation
    search_endpoints    -> find the right operation
    describe_endpoint   -> see its exact params, body and response shape
    cw_get              -> execute any GET (with CW paging/conditions)
    cw_call             -> execute any POST/PATCH/PUT

plus curated read tools (search_tickets, find_company, ...) and five curated
create tools (create_ticket, create_time_entry, create_ticket_note,
create_company, create_contact) — see curated.py / curated_writes.py.

Scope: every documented verb except DELETE. Deletes are not recoverable through
the ConnectWise API, so that verb is excluded from both the catalog
(scope.INCLUDED_METHODS) and the dispatcher (writer.WRITE_METHODS).

Credentials are per request: Authorization Bearer token -> tenant store
(hosted), X-CW-* headers (custom agents), or CW_* env vars (local stdio).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config, curated, curated_writes, onboarding
from .catalog import load_catalog
from .conditions import CONDITIONS_HELP
from .curated import run_get, run_write
from .writer import cw_write

mcp = FastMCP(
    name="connectwise-psa",
    instructions=(
        "Access to ConnectWise Manage (PSA), full API surface. Reads: use "
        "the curated tools (search_tickets, get_ticket, find_company, "
        "list_agreements, recent_time_entries) for common asks; for anything "
        "else use search_endpoints to find the operation, describe_endpoint "
        "for its parameters, then cw_get (see its docs for `conditions` "
        "filter syntax). Writes: the curated create tools (create_ticket, "
        "create_time_entry, create_ticket_note, create_company, "
        "create_contact) cover the common cases; cw_call reaches every other "
        "POST/PATCH/PUT. DELETE is not available on this server. Writes take "
        "effect immediately in the customer's live PSA and most are not "
        "reversible, so confirm the target record with a read before writing, "
        "and prefer PATCH over PUT (PUT replaces the whole record and clears "
        "any field you omit)."
        " Not connected yet? The get_started tool walks new users through"
        " creating a ConnectWise API key and connecting."
    ),
)

catalog = load_catalog()


@mcp.tool
def list_modules() -> dict[str, Any]:
    """List the ConnectWise modules and their operation counts, by verb.

    Use this for orientation before searching (e.g. service, company, finance,
    project, sales, time, schedule, procurement, system).
    """
    return {
        "modules": catalog.modules(),
        "total_operations": len(catalog.endpoints),
        "spec_version": catalog.spec_version,
        "note": "DELETE is not exposed by this server.",
    }


@mcp.tool
def search_endpoints(
    query: str,
    module: str | None = None,
    method: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the catalog by keyword, across every verb.

    Matches operationId, path, summary and category/tag. Intent words in the
    query steer the verb, so "create a ticket" ranks POST first and "close
    ticket" ranks PATCH first; pass `method` to force one. Optionally restrict
    to one `module` (see list_modules). Returns enough detail to often skip
    describe_endpoint.

    Example: search_endpoints("open tickets", module="service", method="GET")
    """
    results = catalog.search(query, module=module, method=method, limit=limit)
    return [
        {
            "operationId": ep.operation_id,
            "method": ep.method,
            "path": ep.path,
            "module": ep.module,
            "tags": ep.tags,
            "summary": ep.summary,
            "path_params": ep.path_params,
            "takes_body": ep.has_body,
        }
        for ep in results
    ]


@mcp.tool
def describe_endpoint(operation_id: str) -> dict[str, Any]:
    """Return the full contract for one operation: params, body, response shape.

    Pass the `operationId` from search_endpoints. Use this when you need the
    exact query params, the request body a write expects, or the structure of
    the returned object. Always call this before a cw_call write you have not
    made before.
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


_CW_CALL_DOC = """Execute a ConnectWise write: POST, PATCH or PUT. WRITES to
    the customer's live PSA.

    `path` is a path from search_endpoints, e.g. "/service/tickets" or
    "/service/tickets/{id}". Fill `{...}` segments via `path_params`
    (e.g. {"id": 123}).

    Verbs:
    - POST creates a record. `body` is the new object, e.g.
      {"summary": "...", "company": {"id": 7}, "board": {"id": 1}}.
    - PATCH updates fields. `body` MUST be a JSON Patch array:
      [{"op": "replace", "path": "/status/id", "value": 5}]
      op is add, replace or remove. This is the right verb for almost every
      update.
    - PUT replaces the entire record. Any field missing from `body` is
      CLEARED, not preserved. Use PATCH unless you genuinely intend to
      overwrite the whole object.

    DELETE is not available on this server; ConnectWise deletes cannot be
    undone through the API. To retire a record, PATCH the flag the object
    uses for that (inactiveFlag, deletedFlag, a closed status, and so on).

    Before writing: call describe_endpoint for the operation to get the body
    shape, and read the target record first so you are certain of the id.
    Writes are immediate and mostly irreversible. Returns the created or
    updated record.
    """


@mcp.tool(description=_CW_CALL_DOC)
async def cw_call(
    method: str,
    path: str,
    body: Any = None,
    path_params: dict[str, Any] | None = None,
) -> Any:
    async def go(client, actor):
        return await cw_write(
            client, method, path, body, path_params=path_params, actor=actor
        )

    return await run_write(go)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Unauthenticated liveness probe for hosted deployments."""
    return JSONResponse({"status": "ok"})


curated.register(mcp)
curated_writes.register(mcp)
onboarding.register(mcp)


def main() -> None:
    """Run the server. Defaults to HTTP; set CW_MCP_TRANSPORT=stdio for local."""
    import logging
    import os

    # Surface audit/info logs on stdout for hosted runtime logs, but never
    # clobber a host application's existing logging configuration.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
        )

    transport = os.getenv("CW_MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host=config.HTTP_HOST, port=config.HTTP_PORT)


if __name__ == "__main__":
    main()
