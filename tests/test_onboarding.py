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
