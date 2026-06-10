"""Per-request ConnectWise credential resolution.

Multi-tenant: credentials are never baked into the running process. They are
resolved per request, in this order:

1. **Bearer token -> tenant store** (hosted HTTP). The request's
   ``Authorization: Bearer <token>`` is looked up in the tenant store — a JSON
   map of token -> ConnectWise credentials, loaded from the file named by
   ``CW_TENANTS_FILE`` (or inline JSON in ``CW_TENANTS``). This is the path
   Claude Desktop uses via ``mcp-remote --header "Authorization: Bearer ..."``.

2. **X-CW-* headers** (custom agents that control headers):
   X-CW-Company-Id, X-CW-Public-Key, X-CW-Private-Key, X-CW-Client-Id,
   and optionally X-CW-Region / X-CW-Host.

3. **CW_* environment variables** (local stdio / single-tenant):
   CW_COMPANY_ID, CW_PUBLIC_KEY, CW_PRIVATE_KEY, CW_CLIENT_ID,
   and optionally CW_REGION / CW_HOST.

Tenant store entry format (keys mirror the env names, lowercased)::

    {
      "<random-token>": {
        "company_id": "...", "public_key": "...", "private_key": "...",
        "client_id": "...", "region": "na"
      }
    }
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from dataclasses import dataclass
from functools import lru_cache

try:  # available only when running under the HTTP transport
    from fastmcp.server.dependencies import get_http_headers
except Exception:  # pragma: no cover - fastmcp always present in practice
    def get_http_headers() -> dict[str, str]:  # type: ignore
        return {}


class MissingCredentials(Exception):
    """Raised when a request lacks the ConnectWise credentials it needs."""


@dataclass(frozen=True)
class CWCredentials:
    company_id: str
    public_key: str
    private_key: str
    client_id: str
    region: str | None = None
    host: str | None = None

    def auth_header(self) -> str:
        """ConnectWise Basic auth: base64(companyId+publicKey : privateKey)."""
        token = f"{self.company_id}+{self.public_key}:{self.private_key}"
        return "Basic " + base64.b64encode(token.encode()).decode()


# ---------------------------------------------------------------- tenant store

_REQUIRED = ("company_id", "public_key", "private_key", "client_id")


@lru_cache(maxsize=1)
def _load_tenants() -> dict[str, dict]:
    raw = None
    path = os.getenv("CW_TENANTS_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    elif os.getenv("CW_TENANTS"):
        raw = os.environ["CW_TENANTS"]
    if not raw:
        return {}
    tenants = json.loads(raw)
    for token, entry in tenants.items():
        missing = [k for k in _REQUIRED if not entry.get(k)]
        if missing:
            raise ValueError(
                f"Tenant store entry for token ...{token[-4:]} is missing {missing}"
            )
    return tenants


def _lookup_token(token: str) -> CWCredentials | None:
    for stored, entry in _load_tenants().items():
        # Constant-time compare so token lookup doesn't leak prefix matches.
        if hmac.compare_digest(stored, token):
            return CWCredentials(
                company_id=entry["company_id"],
                public_key=entry["public_key"],
                private_key=entry["private_key"],
                client_id=entry["client_id"],
                region=entry.get("region"),
                host=entry.get("host"),
            )
    return None


# ------------------------------------------------------------- resolution

def _pick(headers: dict[str, str], header_name: str, env_name: str) -> str | None:
    # get_http_headers() lowercases keys; env is the local fallback.
    return headers.get(header_name.lower()) or os.getenv(env_name)


def get_credentials() -> CWCredentials:
    """Resolve credentials for the current request (see module docs for order)."""
    h = get_http_headers() or {}

    # 1) Bearer token against the tenant store.
    authz = h.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        creds = _lookup_token(token)
        if creds:
            return creds
        if _load_tenants():
            raise MissingCredentials(
                "Bearer token not recognized by this server's tenant store."
            )
        # No tenant store configured: fall through to headers/env.

    # 2) X-CW-* headers, 3) CW_* env.
    company = _pick(h, "X-CW-Company-Id", "CW_COMPANY_ID")
    public = _pick(h, "X-CW-Public-Key", "CW_PUBLIC_KEY")
    private = _pick(h, "X-CW-Private-Key", "CW_PRIVATE_KEY")
    client_id = _pick(h, "X-CW-Client-Id", "CW_CLIENT_ID")
    region = _pick(h, "X-CW-Region", "CW_REGION")
    host = _pick(h, "X-CW-Host", "CW_HOST")

    missing = [
        name
        for name, val in [
            ("company_id", company),
            ("public_key", public),
            ("private_key", private),
            ("client_id", client_id),
        ]
        if not val
    ]
    if missing:
        raise MissingCredentials(
            "Missing ConnectWise credentials: "
            + ", ".join(missing)
            + ". Supply an Authorization: Bearer token (hosted), X-CW-* request "
            "headers (custom agents), or CW_* env vars (local stdio)."
        )

    return CWCredentials(
        company_id=company,  # type: ignore[arg-type]
        public_key=public,  # type: ignore[arg-type]
        private_key=private,  # type: ignore[arg-type]
        client_id=client_id,  # type: ignore[arg-type]
        region=region,
        host=host,
    )
