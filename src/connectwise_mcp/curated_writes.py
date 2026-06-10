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
        f"No member with identifier {identifier!r}. Omit `member` to use "
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


async def _default_comm_type_id(client, *, email: bool) -> int:
    """Id of the instance's default communication type for email or phone.

    Contact ``communicationItems`` require a ``type/id`` and the type NAMES
    are instance-specific, so we look the id up at runtime. This endpoint is
    not in the bundled GET catalog (it exists solely for this internal
    lookup), hence the direct client.get instead of cw_get.
    """
    flag = "emailFlag" if email else "phoneFlag"
    resp = await client.get("/company/communicationTypes", params={"pageSize": 100})
    if resp.status_code >= 400:
        raise ExecutionError(
            f"Could not list communication types ({resp.status_code}): "
            f"{resp.text[:300]}"
        )
    types = [t for t in resp.json() if t.get(flag)]
    if not types:
        kind = "email" if email else "phone"
        raise ResolutionError(
            f"This ConnectWise instance has no {kind} communication type; "
            "create the contact without that field."
        )
    defaults = [t for t in types if t.get("defaultFlag")]
    return (defaults or types)[0]["id"]


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
    """Start time ``hours`` before ``now`` (must be UTC-aware) as CW ISO-Z."""
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
    # Live POST validation requires a site even though the bundled GET schema
    # marks only name+identifier required; CW names the default site "Main".
    body: dict[str, Any] = {
        "name": name,
        "identifier": identifier,
        "site": {"name": "Main"},
    }
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
    email_type_id: int | None = None,
    phone_type_id: int | None = None,
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
                "type": {"id": email_type_id},
                "value": email,
                "defaultFlag": True,
                "communicationType": "Email",
            }
        )
    if phone:
        comm.append(
            {
                "type": {"id": phone_type_id},
                "value": phone,
                "defaultFlag": True,
                "communicationType": "Phone",
            }
        )
    if comm:
        body["communicationItems"] = comm
    return body


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
        member identifier (e.g. "WYoung"); when omitted ConnectWise charges
        the API member, which fails on instances where that member is API-only
        — pass a real member identifier if you get a Security error. billable
        maps to Billable/DoNotBill; omit for the work-type default.
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
            email_type_id = (
                await _default_comm_type_id(client, email=True) if email else None
            )
            phone_type_id = (
                await _default_comm_type_id(client, email=False) if phone else None
            )
            body = _contact_body(
                first_name,
                last_name=last_name,
                company_id=company_id,
                email=email,
                phone=phone,
                title=title,
                email_type_id=email_type_id,
                phone_type_id=phone_type_id,
            )
            return await cw_post(client, "/company/contacts", body)

        return await _run_write(go)
