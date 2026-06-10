"""Writer (POST executor) tests against a mock ConnectWise (no network)."""

import logging

import httpx
import pytest

from connectwise_mcp.executor import ExecutionError
from connectwise_mcp.writer import ALLOWED_POSTS, cw_post


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


async def test_allowlist_blocks_unknown_paths_before_any_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(201, json={"id": 1})

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="not allowed"):
            await cw_post(client, "/service/tickets/{id}", {"summary": "x"}, actor="acme")
        with pytest.raises(ExecutionError, match="not allowed"):
            await cw_post(client, "/system/members", {}, actor="acme")
    assert calls["n"] == 0


async def test_all_five_allowed_paths_post_json_body():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(201, json={"id": 1})

    async with make_client(handler) as client:
        for path, params in [
            ("/service/tickets", None),
            ("/time/entries", None),
            ("/service/tickets/{parentId}/notes", {"parentId": 7}),
            ("/company/companies", None),
            ("/company/contacts", None),
        ]:
            out = await cw_post(client, path, {"a": 1}, path_params=params, actor="acme")
            assert out == {"id": 1}
    assert ("POST", "/api/service/tickets/7/notes") in seen
    assert len(seen) == 5


async def test_missing_path_param_raises():
    async with make_client(lambda r: httpx.Response(201, json={})) as client:
        with pytest.raises(ExecutionError, match="parentId"):
            await cw_post(client, "/service/tickets/{parentId}/notes", {"text": "x"}, actor="acme")


async def test_created_record_info_stripped():
    def handler(request):
        return httpx.Response(
            201, json={"id": 9, "_info": {"u": "x"}, "company": {"id": 1, "_info": {}}}
        )

    async with make_client(handler) as client:
        out = await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    assert out == {"id": 9, "company": {"id": 1}}


async def test_400_surfaces_validation_detail():
    def handler(request):
        return httpx.Response(400, text='{"errors":[{"message":"identifier taken"}]}')

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="identifier taken"):
            await cw_post(client, "/company/companies", {"name": "X"}, actor="acme")


async def test_retry_on_429_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(201, json={"id": 2})

    async with make_client(handler) as client:
        out = await cw_post(client, "/time/entries", {"actualHours": 1}, actor="acme")
    assert calls["n"] == 2
    assert out == {"id": 2}


async def test_no_retry_on_500():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="500"):
            await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    assert calls["n"] == 1


async def test_retry_on_connect_error_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(201, json={"id": 3})

    async with make_client(handler) as client:
        out = await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    assert calls["n"] == 2
    assert out == {"id": 3}


async def test_retry_on_connect_timeout_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("slow connect", request=request)
        return httpx.Response(201, json={"id": 4})

    async with make_client(handler) as client:
        out = await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    assert calls["n"] == 2
    assert out == {"id": 4}


async def test_no_retry_on_read_timeout():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="may or may not have been created"):
            await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    assert calls["n"] == 1


async def test_audit_log_on_success(caplog):
    def handler(request):
        return httpx.Response(201, json={"id": 42})

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m == "WRITE ok company=acme path=/service/tickets id=42" for m in messages
    )


async def test_audit_log_on_failure(caplog):
    def handler(request):
        return httpx.Response(400, text="identifier taken")

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            with pytest.raises(ExecutionError):
                await cw_post(client, "/company/companies", {"name": "X"}, actor="acme")
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("WRITE fail company=acme path=/company/companies status=400")
        for m in messages
    )
