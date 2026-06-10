"""Curated convenience tools for the most common ConnectWise asks.

These sit on top of the generic gateway (`cw_get`) and pre-build the
``conditions`` expressions, so the model can answer everyday questions without
the search -> describe -> execute dance. All paths and field names used here
are verified against the bundled catalog.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import MissingCredentials, get_credentials
from .catalog import load_catalog
from .client import make_client
from .conditions import join_and, quote
from .executor import ExecutionError, cw_get


async def run_get(path: str, **kwargs: Any) -> Any:
    """Resolve credentials and execute one in-scope GET, returning JSON or an
    {"error": ...} dict the model can act on."""
    try:
        creds = get_credentials()
    except MissingCredentials as e:
        return {"error": str(e)}
    try:
        async with make_client(creds) as client:
            return await cw_get(client, load_catalog(), path, **kwargs)
    except ExecutionError as e:
        return {"error": str(e)}


def _utc_days_ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return "[" + dt.strftime("%Y-%m-%dT%H:%M:%SZ") + "]"


def register(mcp) -> None:
    @mcp.tool
    async def search_tickets(
        summary_contains: str | None = None,
        status: str | None = None,
        board: str | None = None,
        company: str | None = None,
        include_closed: bool = False,
        limit: int = 25,
    ) -> Any:
        """Search service tickets with simple filters (no conditions syntax needed).

        - summary_contains: word/phrase in the ticket summary
        - status: status name, e.g. "New", "In Progress"
        - board: service board name
        - company: company name OR identifier (matched against both)
        - include_closed: also return closed tickets (default open only)
        """
        clauses = []
        if not include_closed:
            clauses.append("closedFlag = false")
        if summary_contains:
            clauses.append(f"summary contains {quote(summary_contains)}")
        if status:
            clauses.append(f"status/name = {quote(status)}")
        if board:
            clauses.append(f"board/name = {quote(board)}")
        if company:
            clauses.append(
                f"(company/name contains {quote(company)} "
                f"or company/identifier = {quote(company)})"
            )
        return await run_get(
            "/service/tickets",
            conditions=join_and(*clauses) or None,
            order_by="id desc",
            fields="id,summary,board,status,priority,company,contact,owner,closedFlag",
            page_size=max(1, min(limit, 100)),
        )

    @mcp.tool
    async def get_ticket(ticket_id: int, include_notes: bool = True) -> Any:
        """Fetch one service ticket by id, optionally with its discussion notes."""
        ticket = await run_get(
            "/service/tickets/{id}", path_params={"id": ticket_id}
        )
        if include_notes and isinstance(ticket, dict) and "error" not in ticket:
            notes = await run_get(
                "/service/tickets/{parentId}/notes",
                path_params={"parentId": ticket_id},
                order_by="id desc",
                page_size=25,
            )
            ticket["notes"] = notes
        return ticket

    @mcp.tool
    async def find_company(query: str, limit: int = 10) -> Any:
        """Find companies by name or identifier (partial match, excludes deleted)."""
        conditions = join_and(
            f"(name contains {quote(query)} or identifier contains {quote(query)})",
            "deletedFlag = false",
        )
        return await run_get(
            "/company/companies",
            conditions=conditions,
            order_by="name asc",
            fields="id,identifier,name,status,phoneNumber,website,city,state,types",
            page_size=max(1, min(limit, 100)),
        )

    @mcp.tool
    async def list_agreements(
        company: str | None = None, active_only: bool = True, limit: int = 25
    ) -> Any:
        """List finance agreements, optionally for one company (name/identifier)."""
        clauses = []
        if active_only:
            clauses.append("cancelledFlag = false")
        if company:
            clauses.append(
                f"(company/name contains {quote(company)} "
                f"or company/identifier = {quote(company)})"
            )
        return await run_get(
            "/finance/agreements",
            conditions=join_and(*clauses) or None,
            order_by="id desc",
            fields="id,name,type,company,agreementStatus,startDate,endDate,"
            "cancelledFlag,billAmount,billCycle",
            page_size=max(1, min(limit, 100)),
        )

    @mcp.tool
    async def recent_time_entries(
        days: int = 7,
        member: str | None = None,
        company: str | None = None,
        limit: int = 50,
    ) -> Any:
        """Time entries from the last N days, optionally filtered by member
        identifier and/or company (name/identifier)."""
        clauses = [f"timeStart > {_utc_days_ago(days)}"]
        if member:
            clauses.append(f"member/identifier = {quote(member)}")
        if company:
            clauses.append(
                f"(company/name contains {quote(company)} "
                f"or company/identifier = {quote(company)})"
            )
        return await run_get(
            "/time/entries",
            conditions=join_and(*clauses),
            order_by="timeStart desc",
            fields="id,member,company,chargeToType,chargeToId,workRole,workType,"
            "timeStart,timeEnd,actualHours,billableOption,notes",
            page_size=max(1, min(limit, 200)),
        )
