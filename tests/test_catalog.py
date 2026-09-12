"""Smoke tests for the catalog and executor (no network)."""

import pytest

from connectwise_mcp.catalog import load_catalog
from connectwise_mcp.executor import _fill_path, ExecutionError


def test_catalog_loads_with_every_verb_except_delete():
    cat = load_catalog()
    assert len(cat.endpoints) > 2000
    verbs = {ep.method for ep in cat.endpoints.values()}
    assert verbs == {"GET", "POST", "PATCH", "PUT"}, verbs
    # DELETE is excluded from the catalog, not just from the dispatcher.
    assert not any(ep.method == "DELETE" for ep in cat.endpoints.values())


def test_same_path_carries_several_verbs():
    cat = load_catalog()
    assert cat.by_path("/service/tickets", "GET") is not None
    assert cat.by_path("/service/tickets", "POST") is not None
    assert set(cat.methods_for("/service/tickets/{id}")) >= {"GET", "PATCH"}
    assert "DELETE" not in cat.methods_for("/service/tickets/{id}")


def test_intent_words_steer_the_verb():
    cat = load_catalog()
    assert cat.search("create a ticket", limit=5)[0].method == "POST"
    assert cat.search("close a ticket", limit=5)[0].method == "PATCH"
    assert cat.search("list tickets", limit=5)[0].method == "GET"


def test_search_prefers_the_named_resource_over_the_module_prefix():
    cat = load_catalog()
    top = [h.path for h in cat.search("companies", module="company", limit=3)]
    assert "/company/companies" in top


def test_describe_surfaces_the_request_body_for_writes():
    cat = load_catalog()
    ep = cat.by_path("/service/tickets", "POST")
    d = cat.describe(ep.operation_id)
    assert d["request_body"] is not None
    assert "GET" in d["other_methods_on_path"]


def test_describe_explains_json_patch_for_patch_operations():
    cat = load_catalog()
    ep = cat.by_path("/service/tickets/{id}", "PATCH")
    d = cat.describe(ep.operation_id)
    assert "JSON Patch array" in d["request_body_note"]


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
