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
