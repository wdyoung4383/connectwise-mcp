"""Executor tests against a mock ConnectWise (no network)."""

import httpx
import pytest

from connectwise_mcp.catalog import load_catalog
from connectwise_mcp.executor import ExecutionError, cw_get, strip_info

CATALOG = load_catalog()


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


async def test_list_response_wrapped_with_pagination():
    def handler(request):
        assert request.url.params["pageSize"] == "2"
        return httpx.Response(
            200,
            json=[{"id": 1, "_info": {"x": 1}}, {"id": 2}],
            headers={"Link": '<https://cw.test/api/service/tickets?page=2>; rel="next"'},
        )

    async with make_client(handler) as client:
        out = await cw_get(client, CATALOG, "/service/tickets", page_size=2)
    assert out["count"] == 2
    assert out["has_more"] is True
    assert out["items"][0] == {"id": 1}  # _info stripped


async def test_single_object_passthrough_and_info_kept_on_request():
    def handler(request):
        return httpx.Response(200, json={"id": 5, "_info": {"u": "x"}})

    async with make_client(handler) as client:
        slim = await cw_get(
            client, CATALOG, "/service/tickets/{id}", path_params={"id": 5}
        )
        full = await cw_get(
            client,
            CATALOG,
            "/service/tickets/{id}",
            path_params={"id": 5},
            include_info=True,
        )
    assert "_info" not in slim
    assert full["_info"] == {"u": "x"}


async def test_out_of_scope_path_rejected():
    async with make_client(lambda r: httpx.Response(200, json=[])) as client:
        with pytest.raises(ExecutionError, match="not in this server's read scope"):
            await cw_get(client, CATALOG, "/system/audittrail/nonexistent")


async def test_error_classification_400():
    def handler(request):
        return httpx.Response(400, text="invalid condition")

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="400 Bad Request"):
            await cw_get(client, CATALOG, "/service/tickets")


async def test_retry_on_429_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[])

    async with make_client(handler) as client:
        out = await cw_get(client, CATALOG, "/service/tickets")
    assert calls["n"] == 2
    assert out["count"] == 0
    assert out["has_more"] is False


def test_strip_info_recursive():
    data = {"a": [{"_info": 1, "b": {"_info": 2, "c": 3}}], "_info": 0}
    assert strip_info(data) == {"a": [{"b": {"c": 3}}]}
