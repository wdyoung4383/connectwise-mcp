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
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth, "get_http_headers", lambda **kw: {"authorization": "Bearer tok-abc"}
    )
    c = get_credentials()
    assert (c.company_id, c.region) == ("acme", "eu")


def test_unknown_bearer_token_rejected(monkeypatch):
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-abc": CREDS}))
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth, "get_http_headers", lambda **kw: {"authorization": "Bearer wrong"}
    )
    with pytest.raises(MissingCredentials, match="not recognized"):
        get_credentials()


def test_tenants_file(tmp_path, monkeypatch):
    f = tmp_path / "tenants.json"
    f.write_text(json.dumps({"tok-file": CREDS}))
    monkeypatch.setenv("CW_TENANTS_FILE", str(f))
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth, "get_http_headers", lambda **kw: {"authorization": "Bearer tok-file"}
    )
    assert get_credentials().client_id == "guid-123"


def test_invalid_tenant_entry_rejected(monkeypatch):
    bad = {k: v for k, v in CREDS.items() if k != "private_key"}
    monkeypatch.setenv("CW_TENANTS", json.dumps({"tok-bad": bad}))
    with pytest.raises(ValueError, match="private_key"):
        _load_tenants()


def test_x_cw_headers(monkeypatch):
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
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
    # an HTTP request context exists -> hosted; env must be ignored.
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(auth, "get_http_headers", lambda **kw: {"host": "x"})
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()


def test_x_cw_headers_resolve_even_with_env_set(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "operator")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
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
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda **kw: {
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
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(
        auth, "get_http_headers", lambda **kw: {"authorization": "Bearer tok"}
    )
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()


def test_stripped_headers_do_not_reopen_env_fallback(monkeypatch):
    # Reviewer's bypass: an HTTP request whose headers are all in fastmcp's
    # strip set yields an empty dict. The env fallback must stay closed because
    # a request CONTEXT exists, regardless of the (empty) header dict.
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    monkeypatch.setattr(auth, "get_http_request", lambda: object())
    monkeypatch.setattr(auth, "get_http_headers", lambda **kw: {})
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()


def test_fastmcp_default_view_strips_authorization():
    # Pin the real-world fact that fastmcp's DEFAULT (filtered) header view
    # omits `authorization` (and `host`), while include_all=True keeps them.
    # This guards against a future regression to filtered reads in
    # get_credentials(), which would silently kill the bearer path.
    import inspect

    from fastmcp.server.dependencies import get_http_headers as real_get

    sig = inspect.signature(real_get)
    assert sig.parameters["include_all"].default is False
    src = inspect.getsource(real_get)
    # The default exclude set contains authorization and host.
    assert '"authorization"' in src
    assert '"host"' in src
    # include_all=True empties the exclude set (returns all headers).
    assert "if include_all:" in src
    assert "exclude_headers: set[str] = set()" in src
