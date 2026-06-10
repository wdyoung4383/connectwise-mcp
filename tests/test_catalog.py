"""Smoke tests for the catalog and executor (no network)."""

import pytest

from connectwise_mcp.catalog import load_catalog
from connectwise_mcp.executor import _fill_path, ExecutionError


def test_catalog_loads_and_is_get_only():
    cat = load_catalog()
    assert len(cat.endpoints) > 100
    assert all(ep.method == "GET" for ep in cat.endpoints.values())


def test_modules_present():
    mods = load_catalog().modules()
    for m in ("service", "company", "finance", "time"):
        assert m in mods


def test_search_finds_tickets():
    cat = load_catalog()
    hits = cat.search("tickets", module="service")
    assert any(h.path == "/service/tickets" for h in hits)


def test_describe_returns_params():
    cat = load_catalog()
    hits = cat.search("tickets", module="service")
    d = cat.describe(hits[0].operation_id)
    assert d is not None
    assert "parameters" in d and isinstance(d["parameters"], list)


def test_fill_path_ok_and_missing():
    assert _fill_path("/service/tickets/{id}", {"id": 5}) == "/service/tickets/5"
    with pytest.raises(ExecutionError):
        _fill_path("/service/tickets/{id}", {})
