"""Executes write calls (POST/PATCH/PUT) against ConnectWise.

This module is the only write path in the package. Within those three verbs it
dispatches to any path: there is no per-path allowlist. DELETE is the one verb
excluded, and it is excluded by construction — it is not in WRITE_METHODS, so
there is no code path that issues one. ConnectWise deletes are not recoverable
through the API, which is why that verb stays off the surface entirely.

Note that PUT is still destructive in practice: it replaces the whole record,
so a PUT carrying a partial body blanks every field omitted from it. Prefer
PATCH for field-level changes.

Retry semantics deliberately differ from the GET executor. Writes are not safe
to replay: a retried POST double-creates and a retried JSON Patch "add" appends
twice. So writes retry only when the request provably never reached ConnectWise
(connection never established) or when ConnectWise explicitly asks (429). Never
on 5xx, never on read timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from .executor import ExecutionError, _classify_error, _fill_path, strip_info

_audit = logging.getLogger("connectwise_mcp.audit")

WRITE_METHODS = frozenset({"POST", "PATCH", "PUT"})

_MAX_RETRIES = 3

# Request bodies can carry customer data, so they stay out of the audit log
# unless an operator opts in. With DELETE and PATCH enabled this is the only
# record of what an agent actually changed; turn it on where the runtime log
# destination is an acceptable home for that data.
_LOG_BODIES = os.getenv("CW_AUDIT_BODIES", "").lower() in ("1", "true", "yes")
_BODY_LOG_LIMIT = 2000


def _audit_body(body: Any) -> str:
    if not _LOG_BODIES or body is None:
        return ""
    try:
        rendered = json.dumps(body, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = repr(body)
    return f" body={rendered[:_BODY_LOG_LIMIT]}"


def _classify_write_error(method: str, status: int, body: str) -> str:
    detail = body[:800]
    if status == 400:
        return (
            f"400 Bad Request: ConnectWise rejected the {method} "
            f"(validation). Detail: {detail}"
        )
    if status == 404 and method in ("PATCH", "PUT"):
        return (
            f"404 Not Found: no record at this path to {method.lower()}. "
            f"Confirm the id with a read first. Detail: {detail}"
        )
    if status == 409:
        return (
            f"409 Conflict: ConnectWise refused the {method}, usually a "
            f"duplicate or a record in use. Detail: {detail}"
        )
    return _classify_error(status, body)


async def _send_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    body: Any,
) -> httpx.Response:
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["json"] = body
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Connection never established: the request did not reach CW, so
            # retrying cannot double-apply.
            last_exc = e
            if attempt == _MAX_RETRIES:
                break
            await asyncio.sleep(2**attempt)
            continue
        except httpx.TransportError as e:
            # Sent (or possibly sent) but no response: the write may or may not
            # have landed. Do NOT retry; tell the caller to check first.
            raise ExecutionError(
                f"Network error after sending {method} {url}: {e}. The change "
                "may or may not have been applied — check with a read tool "
                "before retrying."
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


async def cw_write(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: Any = None,
    *,
    path_params: dict[str, Any] | None = None,
    actor: str,
) -> Any:
    """Send a write verb to ``path`` and return the resulting record.

    ``method`` is POST, PATCH or PUT. ``body`` is the request payload: an
    object for POST/PUT, a JSON Patch array for PATCH. DELETE is not supported
    and raises. ``actor`` is the acting tenant's ConnectWise company id, used
    only for the write audit log; tokens and keys are never logged.
    """
    method = method.upper()
    if method == "DELETE":
        raise ExecutionError(
            "DELETE is not available on this server. ConnectWise deletes are "
            "not recoverable through the API, so the verb is excluded by "
            "construction. To retire a record, PATCH its status instead "
            '(e.g. inactiveFlag / deletedFlag where the object supports it).'
        )
    if method not in WRITE_METHODS:
        raise ExecutionError(
            f"{method!r} is not a write verb. Use one of "
            f"{', '.join(sorted(WRITE_METHODS))}, or cw_get for reads."
        )
    if method == "PATCH" and body is not None and not isinstance(body, list):
        raise ExecutionError(
            "ConnectWise PATCH expects a JSON Patch array, e.g. "
            '[{"op": "replace", "path": "/status/id", "value": 5}]. '
            f"Got {type(body).__name__}."
        )
    if method in ("POST", "PUT") and body is None:
        raise ExecutionError(f"{method} requires a body.")
    if method == "PUT" and isinstance(body, dict):
        # PUT replaces the whole record; omitted fields are cleared, not kept.
        _audit.info(
            "WRITE put-replace company=%s path=%s fields=%d",
            actor, path, len(body),
        )

    url = _fill_path(path, path_params)
    try:
        resp = await _send_with_retries(client, method, url, body)
    except ExecutionError as e:
        _audit.info(
            "WRITE fail company=%s method=%s path=%s status=error detail=%.120s%s",
            actor, method, path, str(e), _audit_body(body),
        )
        raise
    if resp.status_code >= 400:
        _audit.info(
            "WRITE fail company=%s method=%s path=%s status=%s detail=%.120s%s",
            actor, method, path, resp.status_code, resp.text, _audit_body(body),
        )
        raise ExecutionError(_classify_write_error(method, resp.status_code, resp.text))

    if resp.status_code == 204 or not resp.content:
        _audit.info(
            "WRITE ok company=%s method=%s path=%s status=%s%s",
            actor, method, path, resp.status_code, _audit_body(body),
        )
        return {"ok": True, "status": resp.status_code, "path": url, "body": None}

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    data = strip_info(data)
    _audit.info(
        "WRITE ok company=%s method=%s path=%s id=%s%s",
        actor, method, path,
        data.get("id") if isinstance(data, dict) else None,
        _audit_body(body),
    )
    return data


async def cw_post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    path_params: dict[str, Any] | None = None,
    actor: str,
) -> Any:
    """POST ``body`` to ``path``. Thin wrapper kept for the curated create tools."""
    return await cw_write(
        client, "POST", path, body, path_params=path_params, actor=actor
    )
