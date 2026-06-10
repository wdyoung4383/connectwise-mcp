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
