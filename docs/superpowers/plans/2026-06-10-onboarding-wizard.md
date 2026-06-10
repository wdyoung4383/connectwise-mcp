# Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-service onboarding (get_started + validate_connection tools), a company-id blocklist kill switch, default-clientId fallback, and a heads-up ticket in Will & Way's ConnectWise on each successful onboarding.

**Architecture:** `auth.py` gains the blocklist and the always-allowed `CW_DEFAULT_CLIENT_ID` fallback. A new `onboarding.py` holds two credential-free tools that probe ConnectWise live and emit a ready-to-paste config snippet; on success it fires a fire-and-forget notification ticket through the existing allowlisted write path using a designated tenant from the tenant store.

**Tech Stack:** Python 3.10+, FastMCP 3.4.2, httpx MockTransport tests, pytest (asyncio_mode=auto).

**Spec:** `docs/superpowers/specs/2026-06-10-onboarding-wizard-design.md`

**Working directory:** `C:\Automation\Mann IT\connectwise-mcp\connectwise-mcp`, branch `onboarding-wizard`. Run tests FROM THE PROJECT ROOT: `.venv\Scripts\python.exe -m pytest`. Current suite: 66 passed.

---

### Task 1: Blocklist + default clientId (`auth.py`)

**Files:**
- Modify: `src/connectwise_mcp/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_auth.py`:

```python
def _set_stdio_env(monkeypatch, company="acme"):
    monkeypatch.setenv("CW_COMPANY_ID", company)
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")


def test_blocklist_blocks_stdio_env_path(monkeypatch):
    _set_stdio_env(monkeypatch)
    monkeypatch.setenv("CW_BLOCKED_COMPANY_IDS", "other, ACME ,third")
    with pytest.raises(MissingCredentials, match="disabled"):
        get_credentials()


def test_blocklist_blocks_header_path(monkeypatch):
    monkeypatch.setenv("CW_BLOCKED_COMPANY_IDS", "acme")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
            "x-cw-company-id": "Acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
            "x-cw-client-id": "guid-123",
        },
    )
    with pytest.raises(MissingCredentials, match="disabled"):
        get_credentials()


def test_blocklist_blocks_bearer_path(monkeypatch):
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-abc": CREDS}))
    monkeypatch.setenv("CW_BLOCKED_COMPANY_IDS", "acme")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth, "get_http_headers", lambda **kw: {"authorization": "Bearer tok-abc"}
    )
    with pytest.raises(MissingCredentials, match="disabled"):
        get_credentials()


def test_blocklist_empty_or_unset_blocks_nothing(monkeypatch):
    _set_stdio_env(monkeypatch)
    monkeypatch.setenv("CW_BLOCKED_COMPANY_IDS", "  ,  ")
    assert get_credentials().company_id == "acme"
    monkeypatch.delenv("CW_BLOCKED_COMPANY_IDS")
    assert get_credentials().company_id == "acme"


def test_default_client_id_fills_gap_under_http(monkeypatch):
    monkeypatch.setenv("CW_DEFAULT_CLIENT_ID", "ww-client-guid")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
        },
    )
    assert get_credentials().client_id == "ww-client-guid"


def test_header_client_id_wins_over_default(monkeypatch):
    monkeypatch.setenv("CW_DEFAULT_CLIENT_ID", "ww-client-guid")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
            "x-cw-client-id": "their-guid",
        },
    )
    assert get_credentials().client_id == "their-guid"


def test_missing_creds_message_mentions_get_started():
    with pytest.raises(MissingCredentials, match="get_started"):
        get_credentials()
```

Also update the `clean_env` autouse fixture's var tuple to include `"CW_BLOCKED_COMPANY_IDS"` and `"CW_DEFAULT_CLIENT_ID"`.

- [ ] **Step 2: Run** `tests/test_auth.py` — new tests FAIL, existing pass.

- [ ] **Step 3: Implement in `src/connectwise_mcp/auth.py`:**

Add after `_lookup_token`:

```python
# ----------------------------------------------------------- blocklist

def _blocked_company_ids() -> frozenset[str]:
    """Company ids denied access (kill switch). Read fresh per call so a
    redeploy with a new CW_BLOCKED_COMPANY_IDS takes effect immediately."""
    raw = os.getenv("CW_BLOCKED_COMPANY_IDS", "")
    return frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )


def _check_blocked(creds: CWCredentials) -> CWCredentials:
    if creds.company_id.lower() in _blocked_company_ids():
        raise MissingCredentials(
            f"Access for company {creds.company_id!r} is disabled. "
            "Contact Will & Way Solutions (book.willandway.solutions)."
        )
    return creds
```

In `get_credentials()`:
1. The bearer-path early return `return creds` becomes `return _check_blocked(creds)`.
2. The final `return CWCredentials(...)` becomes `return _check_blocked(CWCredentials(...))`.
3. After the six `_pick` calls, add the default-clientId fallback:

```python
    # A clientId is integration identity, not an access credential — it is
    # useless without valid keys. The server-level default is therefore
    # deliberately exempt from the HTTP fail-closed rule, so self-service
    # clients never have to register their own clientId.
    if not client_id:
        client_id = os.getenv("CW_DEFAULT_CLIENT_ID")
```

4. Append to the missing-credentials message (after the `hint`):
   `" New here? Call the get_started tool to set up a connection."`
   (Plain concatenation at the end of the existing message; keep `hint` before it.)

- [ ] **Step 4: Full suite from project root** — expect 74 passed (66 + 8).

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: company blocklist kill switch and default clientId fallback"`

---

### Task 2: Wizard tools (`onboarding.py`)

**Files:**
- Create: `src/connectwise_mcp/onboarding.py`
- Test: `tests/test_onboarding.py` (new)

- [ ] **Step 1: Write the failing tests** — create `tests/test_onboarding.py`:

```python
"""Onboarding wizard tests (no network)."""

import json
import logging

import httpx
import pytest

import connectwise_mcp.onboarding as onboarding
from connectwise_mcp.onboarding import _build_config_snippet, _validate


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CW_DEFAULT_CLIENT_ID", "ww-client-guid")
    monkeypatch.delenv("CW_BLOCKED_COMPANY_IDS", raising=False)
    monkeypatch.delenv("CW_NOTIFY_TENANT", raising=False)
    yield


def test_get_started_content():
    out = onboarding.get_started_payload()
    text = json.dumps(out)
    assert "My Account" in text
    assert "API Keys" in text
    assert "only once" in text or "shown only once" in text
    assert "validate_connection" in text
    assert "permissions" in text.lower()


def test_config_snippet_contains_headers():
    snip = _build_config_snippet("acme", "pub", "priv", "na")
    cfg = json.loads(snip)
    args = cfg["mcpServers"]["connectwise"]["args"]
    joined = " ".join(args)
    assert "X-CW-Company-Id: acme" in joined
    assert "X-CW-Public-Key: pub" in joined
    assert "X-CW-Private-Key: priv" in joined
    assert "X-CW-Region: na" in joined
    assert "mcp-remote" in args[0]


async def test_validate_success(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("acme", "pub", "priv", "na")
    assert out["connected"] is True
    assert "config_snippet" in out
    assert "restart" in json.dumps(out["next_steps"]).lower()


async def test_validate_401_guidance(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("acme", "pub", "priv", "na")
    assert out["connected"] is False
    assert "copy/paste" in out["error"] or "rejected" in out["error"]


async def test_validate_403_guidance(monkeypatch):
    def handler(request):
        return httpx.Response(403, text="forbidden")

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("acme", "pub", "priv", "na")
    assert out["connected"] is False
    assert "security role" in out["error"] or "permission" in out["error"].lower()


async def test_validate_blocked_company_no_probe(monkeypatch):
    monkeypatch.setenv("CW_BLOCKED_COMPANY_IDS", "acme")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("Acme", "pub", "priv", "na")
    assert out["connected"] is False
    assert "disabled" in out["error"]
    assert calls["n"] == 0


async def test_validate_missing_default_client_id(monkeypatch):
    monkeypatch.delenv("CW_DEFAULT_CLIENT_ID")
    out = await _validate("acme", "pub", "priv", "na")
    assert out["connected"] is False
    assert "CW_DEFAULT_CLIENT_ID" in out["error"]


async def test_no_private_key_in_logs(monkeypatch, caplog):
    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    with caplog.at_level(logging.DEBUG):
        await _validate("acme", "pub", "SECRETPRIV", "na")
    for record in caplog.records:
        assert "SECRETPRIV" not in record.getMessage()
```

- [ ] **Step 2: Run** — ModuleNotFoundError for `connectwise_mcp.onboarding`.

- [ ] **Step 3: Create `src/connectwise_mcp/onboarding.py`:**

```python
"""Self-service onboarding: walk a new user through connecting.

Two credential-free tools. ``get_started`` returns the walkthrough for
creating a personal ConnectWise API key (My Account -> API Keys — the key
inherits the member's own permissions, so the connector can never do more
than the person who connected it). ``validate_connection`` probes the
submitted keys live and, on success, returns a ready-to-paste Claude
Desktop config plus fires a heads-up ticket to Will & Way (fire-and-forget,
see notify module). Key material is never logged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .auth import CWCredentials, _blocked_company_ids
from .client import make_client
from .executor import strip_info

_audit = logging.getLogger("connectwise_mcp.audit")
_log = logging.getLogger(__name__)

GATEWAY_URL = os.getenv(
    "CW_PUBLIC_MCP_URL",
    "https://willandway-cw-mcp-kzcy7.ondigitalocean.app/mcp",
)

_REGIONS = ("na", "eu", "au", "aus", "za")


def get_started_payload() -> dict[str, Any]:
    return {
        "title": "Connect Claude to your ConnectWise",
        "steps": [
            "Sign in to ConnectWise Manage in your browser.",
            "Click your avatar or initials (top right) and choose "
            "'My Account'.",
            "Open the 'API Keys' tab and click '+' to create a new key. "
            "Description: 'Will & Way Claude Connector'. Save.",
            "Copy the public key AND the private key now — the private key "
            "is shown only once. If you lose it, delete the key and make a "
            "new one.",
            "Note your login company id: it is the 'Company' value you type "
            "on the ConnectWise sign-in screen.",
            "Your region is where your ConnectWise lives: na (North "
            "America), eu, au, aus, or za. If unsure, it is probably na.",
        ],
        "good_to_know": (
            "The key inherits YOUR permissions — Claude will only be able "
            "to see and do what your own ConnectWise account allows."
        ),
        "next": (
            "Call validate_connection with company_id, public_key, "
            "private_key, and region. I will test the keys live and give "
            "you the exact configuration to paste."
        ),
    }


def _build_config_snippet(
    company_id: str, public_key: str, private_key: str, region: str
) -> str:
    cfg = {
        "mcpServers": {
            "connectwise": {
                "command": "npx",
                "args": [
                    "mcp-remote",
                    GATEWAY_URL,
                    "--header", f"X-CW-Company-Id: {company_id}",
                    "--header", f"X-CW-Public-Key: {public_key}",
                    "--header", f"X-CW-Private-Key: {private_key}",
                    "--header", f"X-CW-Region: {region}",
                ],
            }
        }
    }
    return json.dumps(cfg, indent=2)


async def _probe(client, path: str) -> tuple[bool, str]:
    """One read probe. Returns (ok, guidance-on-failure)."""
    resp = await client.get(path, params={"pageSize": 1})
    if resp.status_code == 401:
        return False, (
            "ConnectWise rejected the keys (401). Check for copy/paste "
            "errors — no extra spaces — and that you copied the PRIVATE "
            "key, not the public one twice. If in doubt, delete the key in "
            "My Account -> API Keys and create a fresh one."
        )
    if resp.status_code == 403:
        return False, (
            "ConnectWise refused access (403). Your account's security "
            "role does not allow API access to this area — ask your "
            "ConnectWise administrator to review your role's API "
            "permissions."
        )
    if resp.status_code >= 400:
        return False, (
            f"ConnectWise returned {resp.status_code}: {resp.text[:200]}"
        )
    return True, ""


async def _validate(
    company_id: str, public_key: str, private_key: str, region: str = "na"
) -> dict[str, Any]:
    company_id = company_id.strip()
    region = (region or "na").strip().lower()
    if region not in _REGIONS:
        return {
            "connected": False,
            "error": f"Unknown region {region!r}. Use one of {_REGIONS}.",
        }
    if company_id.lower() in _blocked_company_ids():
        return {
            "connected": False,
            "error": (
                f"Access for company {company_id!r} is disabled. Contact "
                "Will & Way Solutions (book.willandway.solutions)."
            ),
        }
    client_id = os.getenv("CW_DEFAULT_CLIENT_ID")
    if not client_id:
        return {
            "connected": False,
            "error": (
                "Server misconfiguration: CW_DEFAULT_CLIENT_ID is not set. "
                "Contact Will & Way Solutions."
            ),
        }
    creds = CWCredentials(
        company_id=company_id,
        public_key=public_key.strip(),
        private_key=private_key.strip(),
        client_id=client_id,
        region=region,
    )
    try:
        async with make_client(creds) as client:
            for path in ("/service/boards", "/company/companies"):
                ok, guidance = await _probe(client, path)
                if not ok:
                    _audit.info(
                        "ONBOARD failed company=%s region=%s", company_id, region
                    )
                    return {"connected": False, "error": guidance}
    except Exception as e:  # network-level
        _audit.info("ONBOARD failed company=%s region=%s", company_id, region)
        return {
            "connected": False,
            "error": f"Could not reach ConnectWise ({type(e).__name__}). "
            "Check the region and try again.",
        }

    await _notify_onboarding(company_id, region)
    _audit.info("ONBOARD validated company=%s region=%s", company_id, region)
    return {
        "connected": True,
        "config_snippet": _build_config_snippet(
            company_id, public_key.strip(), private_key.strip(), region
        ),
        "next_steps": [
            "Open your Claude Desktop configuration (Settings -> Developer "
            "-> Edit Config) and replace the current 'connectwise' entry "
            "with the config_snippet above.",
            "Fully restart Claude Desktop (quit from the system tray / "
            "dock, then reopen).",
            "Ask: 'What service boards do we have?' to confirm.",
        ],
        "note": (
            "Your keys now live only in your own configuration file. Treat "
            "that file like a password."
        ),
    }


async def _notify_onboarding(company_id: str, region: str) -> None:
    """Heads-up ticket in Will & Way's ConnectWise. Never raises."""
    from .auth import _lookup_token
    from .writer import cw_post

    token = os.getenv("CW_NOTIFY_TENANT")
    board = os.getenv("CW_NOTIFY_BOARD", "Service Desk")
    company = os.getenv("CW_NOTIFY_COMPANY")
    if not token or not company:
        _log.info("onboarding notification skipped (notify envs unset)")
        return
    try:
        creds = _lookup_token(token)
        if creds is None:
            raise ValueError("CW_NOTIFY_TENANT token not in tenant store")
        from .catalog import load_catalog
        from .executor import cw_get

        async with make_client(creds) as client:
            companies = await cw_get(
                client,
                load_catalog(),
                "/company/companies",
                conditions=f"identifier = '{company}'",
                fields="id",
                page_size=1,
            )
            items = companies.get("items") or []
            if not items:
                raise ValueError(f"notify company {company!r} not found")
            boards = await cw_get(
                client,
                load_catalog(),
                "/service/boards",
                conditions=f"name = '{board}'",
                fields="id",
                page_size=1,
            )
            bitems = boards.get("items") or []
            if not bitems:
                raise ValueError(f"notify board {board!r} not found")
            body = {
                "summary": f"New Claude connector onboarding: {company_id}",
                "company": {"id": items[0]["id"]},
                "board": {"id": bitems[0]["id"]},
                "initialDescription": (
                    f"A user from company id '{company_id}' (region "
                    f"{region}) validated a connection to the ConnectWise "
                    "MCP gateway."
                ),
            }
            created = await cw_post(
                client, "/service/tickets", body, actor="onboarding"
            )
            created = strip_info(created)
            _log.info(
                "onboarding notification ticket %s created",
                created.get("id") if isinstance(created, dict) else "?",
            )
    except Exception as e:
        _log.warning("onboarding notification failed: %s", e)


def register(mcp) -> None:
    @mcp.tool
    async def get_started() -> Any:
        """Start here if you are NOT connected to ConnectWise yet.

        Returns a step-by-step walkthrough for creating a personal
        ConnectWise API key (it inherits your own permissions) and
        connecting Claude Desktop to the Will & Way gateway. No credentials
        required to call this.
        """
        return get_started_payload()

    @mcp.tool
    async def validate_connection(
        company_id: str,
        public_key: str,
        private_key: str,
        region: str = "na",
    ) -> Any:
        """Test ConnectWise API keys live and get your ready-to-paste
        Claude Desktop configuration. Use after following get_started.

        - company_id: the company you type on the ConnectWise login screen
        - public_key / private_key: from My Account -> API Keys
        - region: na (default), eu, au, aus, or za
        """
        return await _validate(company_id, public_key, private_key, region)
```

- [ ] **Step 4: Run** `tests/test_onboarding.py` — expect 8 passed; full suite 82.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: self-service onboarding wizard tools"`

---

### Task 3: Notification tests + server wiring

**Files:**
- Modify: `src/connectwise_mcp/server.py`
- Test: `tests/test_onboarding.py` (append), `tests/test_tools.py` (EXPECTED_TOOLS)

- [ ] **Step 1: Write the failing tests.**

In `tests/test_tools.py` add `"get_started"` and `"validate_connection"` to `EXPECTED_TOOLS` (now 16 names).

Append to `tests/test_onboarding.py`:

```python
async def test_notification_created_on_success(monkeypatch):
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"id": 555})
        path = request.url.path
        if path.endswith("/company/companies"):
            return httpx.Response(200, json=[{"id": 42}])
        if path.endswith("/service/boards"):
            return httpx.Response(200, json=[{"id": 27}])
        return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setenv("CW_TENANTS", json.dumps({
        "notify-tok": {"company_id": "willandway", "public_key": "p",
                        "private_key": "s", "client_id": "c", "region": "na"}
    }))
    monkeypatch.setenv("CW_NOTIFY_TENANT", "notify-tok")
    monkeypatch.setenv("CW_NOTIFY_COMPANY", "WillAndWay")
    monkeypatch.setenv("CW_NOTIFY_BOARD", "Service Desk")
    from connectwise_mcp.auth import _load_tenants
    _load_tenants.cache_clear()

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("newclient", "pub", "priv", "na")
    assert out["connected"] is True
    assert len(posted) == 1
    assert posted[0]["summary"] == "New Claude connector onboarding: newclient"
    assert "priv" not in json.dumps(posted[0])
    _load_tenants.cache_clear()


async def test_notification_failure_does_not_break_onboarding(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setenv("CW_TENANTS", json.dumps({
        "notify-tok": {"company_id": "willandway", "public_key": "p",
                        "private_key": "s", "client_id": "c", "region": "na"}
    }))
    monkeypatch.setenv("CW_NOTIFY_TENANT", "notify-tok")
    monkeypatch.setenv("CW_NOTIFY_COMPANY", "WillAndWay")
    from connectwise_mcp.auth import _load_tenants
    _load_tenants.cache_clear()

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("newclient", "pub", "priv", "na")
    assert out["connected"] is True
    _load_tenants.cache_clear()


async def test_notification_skipped_when_unconfigured(monkeypatch):
    calls = {"posts": 0}

    def handler(request):
        if request.method == "POST":
            calls["posts"] += 1
        return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setattr(
        onboarding, "make_client", lambda creds: make_client(handler)
    )
    out = await _validate("newclient", "pub", "priv", "na")
    assert out["connected"] is True
    assert calls["posts"] == 0
```

- [ ] **Step 2: Run** — the EXPECTED_TOOLS test and (depending on Task 2 ordering) notification tests FAIL.

- [ ] **Step 3: Wire into `src/connectwise_mcp/server.py`:**
1. Import: `from . import config, curated, curated_writes, onboarding`
2. After `curated_writes.register(mcp)` add `onboarding.register(mcp)`
3. Append to the `instructions` string: `" Not connected yet? The get_started tool walks new users through creating a ConnectWise API key and connecting."`

- [ ] **Step 4: Full suite** — expect 85 passed.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: onboarding notification ticket + server wiring"`

---

### Task 4: Deployment artifacts + docs

**Files:**
- Modify: `.do/app.yaml`, `docs/DEPLOY.md`, `README.md`

- [ ] **Step 1: `.do/app.yaml`** — add to the `envs:` list:

```yaml
      - key: CW_DEFAULT_CLIENT_ID
        scope: RUN_TIME
        type: SECRET
        value: "REPLACE_IN_DO_CONSOLE"
      - key: CW_BLOCKED_COMPANY_IDS
        scope: RUN_TIME
        value: ""
      - key: CW_NOTIFY_TENANT
        scope: RUN_TIME
        type: SECRET
        value: "REPLACE_IN_DO_CONSOLE"
      - key: CW_NOTIFY_COMPANY
        scope: RUN_TIME
        value: ""
      - key: CW_NOTIFY_BOARD
        scope: RUN_TIME
        value: "Service Desk"
```

- [ ] **Step 2: `docs/DEPLOY.md`** — add a numbered section "Self-service onboarding & the kill switch" documenting each variable: CW_DEFAULT_CLIENT_ID (your ConnectWise integration clientId — used for all self-service clients), CW_BLOCKED_COMPANY_IDS (comma-separated company ids; editing it triggers a redeploy, which IS the revocation latency, ~1-2 min), CW_NOTIFY_TENANT (a token that must exist in CW_TENANTS; its credentials file the heads-up ticket), CW_NOTIFY_COMPANY (company identifier in your CW the ticket is filed against), CW_NOTIFY_BOARD (board name). State the self-service flow in two sentences (client connects bare, get_started → validate_connection → pastes config with X-CW headers).

- [ ] **Step 3: README.md** — after the credentials section, add a short "Self-service onboarding" paragraph: new users connect with no credentials, call `get_started`, validate keys live via `validate_connection`, and receive an X-CW-headers config; operators can cut off any company via `CW_BLOCKED_COMPANY_IDS`; successful onboardings file a heads-up ticket.

- [ ] **Step 4: Verify** — full suite still 85 passed; `python -c "import yaml..."` parse check on app.yaml.

- [ ] **Step 5: Commit** `git add -A && git commit -m "docs: deployment config for onboarding wizard, blocklist, notifications"`

---

### Task 5: Ship

- [ ] Final holistic review (subagent) of the whole branch range
- [ ] Merge to master, push branch + master, create PR record
- [ ] Set the new env vars in the DO console (user does secrets; walk them through): CW_DEFAULT_CLIENT_ID = the existing clientId, CW_NOTIFY_TENANT = existing tenant token, CW_NOTIFY_COMPANY + CW_NOTIFY_BOARD
- [ ] Live acceptance: bare connection → get_started → validate_connection with a real personal API key → confirm config snippet works + heads-up ticket created + ONBOARD audit line; blocklist round-trip
