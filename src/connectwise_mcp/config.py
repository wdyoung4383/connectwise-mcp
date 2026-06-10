"""Static configuration: region -> base URL, defaults.

Per-tenant credentials are NOT stored here; they arrive per request (see
``auth.py``). This module only holds non-secret, environment-level defaults.
"""

from __future__ import annotations

import os

# ConnectWise hosts by region code. The OpenAPI spec hard-codes ``na``; real
# deployments must pick the region their CW instance lives in (or a fully
# self-hosted host via the X-CW-Host header).
REGION_HOSTS = {
    "na": "na.myconnectwise.net",
    "eu": "eu.myconnectwise.net",
    "au": "au.myconnectwise.net",
    "aus": "aus.myconnectwise.net",
    "za": "za.myconnectwise.net",
}

API_PATH = "/v4_6_release/apis/3.0"

# Default page size for list endpoints. Kept small so responses stay within a
# model-friendly size; callers can override per request.
DEFAULT_PAGE_SIZE = int(os.getenv("CW_DEFAULT_PAGE_SIZE", "25"))
MAX_PAGE_SIZE = 1000  # ConnectWise hard limit

# HTTP transport binding (only used when running as a hosted server).
HTTP_HOST = os.getenv("CW_MCP_HOST", "127.0.0.1")
HTTP_PORT = int(os.getenv("CW_MCP_PORT", "8000"))


def base_url(region: str | None = None, host: str | None = None) -> str:
    """Resolve the API base URL from a region code or an explicit host."""
    if host:
        host = host.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{host}{API_PATH}"
    region = (region or "na").lower()
    if region not in REGION_HOSTS:
        raise ValueError(
            f"Unknown CW region {region!r}. Known: {sorted(REGION_HOSTS)} "
            "(or pass an explicit host via X-CW-Host)."
        )
    return f"https://{REGION_HOSTS[region]}{API_PATH}"
