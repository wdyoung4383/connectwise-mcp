"""Tenant store and credential resolution tests."""

import json

import pytest

from connectwise_mcp import auth
from connectwise_mcp.auth import (
    CWCredentials,
    MissingCredentials,
    _load_tenants,
    get_credentials,
)

CREDS = {
    "company_id": "acme",
    "public_key": "pub",
    "private_key": "priv",
    "client_id": "guid-123",
    "region": "eu",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "CW_TENANTS",
        "CW_TENANTS_FILE",
        "CW_COMPANY_ID",
        "CW_PUBLIC_KEY",
        "CW_PRIVATE_KEY",
        "CW_CLIENT_ID",
        "CW_REGION",
        "CW_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    _load_tenants.cache_clear()
    yield
    _load_tenants.cache_clear()


def test_auth_header_format():
    c = CWCredentials(**CREDS)
    import base64

    decoded = base64.b64decode(c.auth_header().split()[1]).decode()
    assert decoded == "acme+pub:priv"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    c = get_credentials()
    assert c.company_id == "acme"


def test_missing_creds_message():
    with pytest.raises(MissingCredentials, match="company_id"):
        get_credentials()


def test_bearer_token_lookup(monkeypatch):
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-abc": CREDS}))
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer tok-abc"}
    )
    c = get_credentials()
    assert (c.company_id, c.region) == ("acme", "eu")


def test_unknown_bearer_token_rejected(monkeypatch):
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-abc": CREDS}))
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer wrong"}
    )
    with pytest.raises(MissingCredentials, match="not recognized"):
        get_credentials()


def test_tenants_file(tmp_path, monkeypatch):
    f = tmp_path / "tenants.json"
    f.write_text(json.dumps({"tok-file": CREDS}))
    monkeypatch.setenv("CW_TENANTS_FILE", str(f))
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer tok-file"}
    )
    assert get_credentials().client_id == "guid-123"


def test_invalid_tenant_entry_rejected(monkeypatch):
    bad = {k: v for k, v in CREDS.items() if k != "private_key"}
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-bad": bad}))
    with pytest.raises(ValueError, match="private_key"):
        _load_tenants()


def test_x_cw_headers(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
            "x-cw-client-id": "guid-123",
            "x-cw-region": "au",
        },
    )
    c = get_credentials()
    assert c.region == "au"


def test_env_fallback_ignored_for_http_requests(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    # any HTTP request carries headers (e.g. host) -> hosted context
    monkeypatch.setattr(auth, "get_http_headers", lambda: {"host": "x"})
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()


def test_x_cw_headers_resolve_even_with_env_set(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "operator")
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
            "x-cw-client-id": "guid-123",
        },
    )
    assert get_credentials().company_id == "acme"


def test_partial_x_cw_headers_do_not_borrow_env(monkeypatch):
    # missing private key must NOT be silently filled from the operator env
    monkeypatch.setenv("CW_PRIVATE_KEY", "operator-secret")
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-client-id": "guid-123",
        },
    )
    with pytest.raises(MissingCredentials, match="private_key"):
        get_credentials()


def test_bearer_without_tenant_store_does_not_fall_to_env(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer tok"}
    )
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()
