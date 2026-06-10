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
