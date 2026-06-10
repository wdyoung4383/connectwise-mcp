"""Executes GET calls against ConnectWise. Read-only by construction.

Only GET is implemented here. There is intentionally no create/update/delete
path, so the read-only guarantee is enforced by the absence of code, not by a
flag that could be flipped.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from .catalog import Catalog
from .config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

_PATH_VAR = re.compile(r"\{([^}]+)\}")

# Retry on throttling and transient server errors.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class ExecutionError(Exception):
    pass


def _fill_path(path: str, path_params: dict[str, Any] | None) -> str:
    path_params = path_params or {}
    needed = _PATH_VAR.findall(path)
    missing = [v for v in needed if v not in path_params]
    if missing:
        raise ExecutionError(
            f"Missing path parameter(s) {missing} for {path}. "
            f"Provide them in path_params."
        )
    return _PATH_VAR.sub(lambda m: str(path_params[m.group(1)]), path)


def _classify_error(status: int, body: str) -> str:
    detail = body[:800]
    if status == 401:
        return (
            "401 Unauthorized: ConnectWise rejected the credentials. Check "
            "company id, public/private key, and that the API member is active. "
            f"Detail: {detail}"
        )
    if status == 403:
        return (
            "403 Forbidden: the API member lacks permission for this endpoint "
            f"(check its security role) or the clientId is invalid. Detail: {detail}"
        )
    if status == 400:
        return (
            "400 Bad Request: usually an invalid `conditions`/`orderBy`/`fields` "
            "expression. Check quoting ('strings'), date brackets "
            f"([2026-01-01T00:00:00Z]) and field names. Detail: {detail}"
        )
    if status == 404:
        return f"404 Not Found: no record at this path/id. Detail: {detail}"
    if status == 429:
        return f"429 Too Many Requests: ConnectWise is throttling. Detail: {detail}"
    return f"ConnectWise returned {status}: {detail}"


def strip_info(obj: Any) -> Any:
    """Recursively remove ConnectWise ``_info`` metadata blocks (link noise)."""
    if isinstance(obj, dict):
        return {k: strip_info(v) for k, v in obj.items() if k != "_info"}
    if isinstance(obj, list):
        return [strip_info(v) for v in obj]
    return obj


async def _get_with_retries(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
        except httpx.TransportError as e:
            last_exc = e
            if attempt == _MAX_RETRIES:
                break
            await asyncio.sleep(2**attempt)
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            await asyncio.sleep(min(delay, 30.0))
            continue
        return resp
    raise ExecutionError(f"Network error talking to ConnectWise: {last_exc}")


async def cw_get(
    client: httpx.AsyncClient,
    catalog: Catalog,
    path: str,
    *,
    path_params: dict[str, Any] | None = None,
    conditions: str | None = None,
    child_conditions: str | None = None,
    custom_field_conditions: str | None = None,
    order_by: str | None = None,
    fields: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    page_id: int | None = None,
    include_info: bool = False,
    extra_query: dict[str, Any] | None = None,
) -> Any:
    """Execute a GET against a known in-scope path.

    List responses are wrapped as ``{"items", "count", "page", "has_more"}``
    using ConnectWise's ``Link`` headers to detect further pages; single-object
    responses are returned as-is. ``_info`` metadata is stripped unless
    ``include_info`` is set.
    """
    ep = catalog.by_path(path)
    if ep is None:
        raise ExecutionError(
            f"Path {path!r} is not in this server's read scope. "
            "Use search_endpoints to find a valid path."
        )

    url = _fill_path(path, path_params)

    ps = page_size if page_size is not None else DEFAULT_PAGE_SIZE
    ps = max(1, min(ps, MAX_PAGE_SIZE))

    query: dict[str, Any] = {"pageSize": ps}
    if conditions:
        query["conditions"] = conditions
    if child_conditions:
        query["childConditions"] = child_conditions
    if custom_field_conditions:
        query["customFieldConditions"] = custom_field_conditions
    if order_by:
        query["orderBy"] = order_by
    if fields:
        query["fields"] = fields
    if page is not None:
        query["page"] = page
    if page_id is not None:
        query["pageId"] = page_id
    if extra_query:
        query.update(extra_query)

    resp = await _get_with_retries(client, url, query)
    if resp.status_code >= 400:
        raise ExecutionError(_classify_error(resp.status_code, resp.text))

    try:
        data = resp.json()
    except ValueError:
        return {"raw": resp.text}

    if not include_info:
        data = strip_info(data)

    if isinstance(data, list):
        # httpx parses RFC-5988 Link headers (CW uses them to signal paging).
        has_more = "next" in resp.links or len(data) >= ps
        return {
            "items": data,
            "count": len(data),
            "page": page if page is not None else 1,
            "page_size": ps,
            "has_more": has_more,
            "hint": (
                "More results exist: request the next `page` (or narrow with "
                "`conditions`). Use the matching /count endpoint for totals."
                if has_more
                else None
            ),
        }
    return data
