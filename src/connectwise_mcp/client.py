"""Builds a per-request httpx client from ConnectWise credentials."""

from __future__ import annotations

import httpx

from .auth import CWCredentials
from .config import base_url


def make_client(creds: CWCredentials, timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an authenticated async client for one ConnectWise tenant.

    A fresh client per request keeps tenants isolated; callers should use it as
    an async context manager so connections are closed promptly.
    """
    return httpx.AsyncClient(
        base_url=base_url(region=creds.region, host=creds.host),
        headers={
            "Authorization": creds.auth_header(),
            "clientId": creds.client_id,  # required on every CW call
            "Accept": "application/json",
        },
        timeout=timeout,
    )
