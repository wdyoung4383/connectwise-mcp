"""Writer tests against a mock ConnectWise (no network).

Covers the generic POST/PATCH/PUT dispatcher and the DELETE exclusion.
"""

import json
import logging

import httpx
import pytest

from connectwise_mcp.executor import ExecutionError
from connectwise_mcp import writer
from connectwise_mcp.writer import WRITE_METHODS, cw_post, cw_write


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


def test_delete_is_not_a_write_method():
    """DELETE is excluded by construction, not by a runtime check."""
    assert "DELETE" not in WRITE_METHODS
    assert WRITE_METHODS == {"POST", "PATCH", "PUT"}


async def test_delete_rejected_before_any_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(204)

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="DELETE is not available"):
            await cw_write(client, "DELETE", "/service/tickets/{id}",
                           path_params={"id": 1}, actor="acme")
    assert calls["n"] == 0


async def test_unknown_verb_rejected_before_any_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="not a write verb"):
            await cw_write(client, "HEAD", "/service/tickets", {}, actor="acme")
    assert calls["n"] == 0


async def test_any_path_is_writable_now():
    """No per-path allowlist: paths the old writer refused now go through."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(201, json={"id": 1})

    async with make_client(handler) as client:
        await cw_write(client, "POST", "/system/members", {"identifier": "x"}, actor="acme")
        await cw_write(client, "POST", "/finance/agreements", {"name": "x"}, actor="acme")
    assert ("POST", "/api/system/members") in seen
    assert ("POST", "/api/finance/agreements") in seen


async def test_patch_sends_json_patch_array():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 5, "status": {"id": 9}})

    ops = [{"op": "replace", "path": "/status/id", "value": 9}]
    async with make_client(handler) as client:
        out = await cw_write(
            client, "PATCH", "/service/tickets/{id}", ops,
            path_params={"id": 5}, actor="acme",
        )
    assert seen["method"] == "PATCH"
    assert seen["body"] == ops
    assert out == {"id": 5, "status": {"id": 9}}


async def test_patch_rejects_non_array_body_before_any_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="JSON Patch array"):
            await cw_write(client, "PATCH", "/service/tickets/{id}",
                           {"status": {"id": 9}}, path_params={"id": 5}, actor="acme")
    assert calls["n"] == 0


async def test_put_sends_full_object():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 5})

    async with make_client(handler) as client:
        await cw_write(client, "PUT", "/service/tickets/{id}",
                       {"summary": "s", "board": {"id": 1}},
                       path_params={"id": 5}, actor="acme")
    assert seen["method"] == "PUT"
    assert seen["body"] == {"summary": "s", "board": {"id": 1}}


async def test_post_and_put_require_a_body():
    async with make_client(lambda r: httpx.Response(201, json={})) as client:
        for verb in ("POST", "PUT"):
            with pytest.raises(ExecutionError, match="requires a body"):
                await cw_write(client, verb, "/service/tickets", None, actor="acme")


async def test_404_on_update_tells_caller_to_confirm_the_id():
    def handler(request):
        return httpx.Response(404, text="not found")

    async with make_client(handler) as client:
        with pytest.raises(ExecutionError, match="Confirm the id with a read first"):
            await cw_write(client, "PATCH", "/service/tickets/{id}",
                           [{"op": "replace", "path": "/x", "value": 1}],
                           path_params={"id": 999}, actor="acme")


async def test_bodies_are_not_logged_by_default(caplog, monkeypatch):
    monkeypatch.setattr(writer, "_LOG_BODIES", False)

    def handler(request):
        return httpx.Response(201, json={"id": 1})

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            await cw_post(client, "/company/contacts",
                          {"firstName": "Jane", "email": "jane@acme.test"},
                          actor="acme")
    assert not any("jane@acme.test" in r.getMessage() for r in caplog.records)


async def test_bodies_logged_when_opted_in(caplog, monkeypatch):
    monkeypatch.setattr(writer, "_LOG_BODIES", True)

    def handler(request):
        return httpx.Response(201, json={"id": 1})

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            await cw_post(client, "/company/contacts", {"firstName": "Jane"}, actor="acme")
    assert any('body={"firstName":"Jane"}' in r.getMessage() for r in caplog.records)


async def test_curated_create_paths_still_post_json_body():
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
        with pytest.raises(ExecutionError, match="may or may not have been applied"):
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
        m == "WRITE ok company=acme method=POST path=/service/tickets id=42"
        for m in messages
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
        m.startswith(
            "WRITE fail company=acme method=POST path=/company/companies status=400"
        )
        for m in messages
    )
