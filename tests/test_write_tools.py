"""Write-tool tests: resolution helpers and body builders (no network)."""

import json

import httpx
import pytest

from connectwise_mcp.curated_writes import (
    ResolutionError,
    _resolve_board,
    _resolve_company,
    _resolve_contact,
    _resolve_member,
)


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cw.test/api"
    )


def json_response(items):
    return httpx.Response(200, json=items)


async def test_resolve_company_exact_identifier_wins():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier = 'SafePointIT'" in cond:
            return json_response([{"id": 19332, "identifier": "SafePointIT", "name": "SafePoint IT"}])
        raise AssertionError(f"unexpected conditions: {cond}")

    async with make_client(handler) as client:
        assert await _resolve_company(client, "SafePointIT") == 19332


async def test_resolve_company_falls_back_to_name_contains():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier =" in cond:
            return json_response([])
        assert "name contains 'SafePoint'" in cond
        return json_response([{"id": 19332, "identifier": "SafePointIT", "name": "SafePoint IT"}])

    async with make_client(handler) as client:
        assert await _resolve_company(client, "SafePoint") == 19332


async def test_resolve_company_ambiguous_lists_candidates():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier =" in cond:
            return json_response([])
        return json_response(
            [
                {"id": 1, "identifier": "ACME", "name": "Acme Inc"},
                {"id": 2, "identifier": "ACMEUK", "name": "Acme UK"},
            ]
        )

    async with make_client(handler) as client:
        with pytest.raises(ResolutionError) as exc:
            await _resolve_company(client, "Acme")
    assert "ACMEUK" in str(exc.value)


async def test_resolve_company_no_match():
    def handler(request):
        return json_response([])

    async with make_client(handler) as client:
        with pytest.raises(ResolutionError, match="No company matched"):
            await _resolve_company(client, "Nope")


async def test_resolve_board_found_and_not_found():
    def handler(request):
        cond = request.url.params.get("conditions", "")
        if "name = 'Service Desk'" in cond:
            return json_response([{"id": 27, "name": "Service Desk"}])
        if "name = 'Wrong'" in cond:
            return json_response([])
        # the fallback "list all boards" request has no conditions
        return json_response([{"id": 27, "name": "Service Desk"}, {"id": 28, "name": "Projects"}])

    async with make_client(handler) as client:
        assert await _resolve_board(client, "Service Desk") == 27
        with pytest.raises(ResolutionError, match="Projects"):
            await _resolve_board(client, "Wrong")


async def test_resolve_member():
    def handler(request):
        cond = request.url.params["conditions"]
        if "identifier = 'wyoung'" in cond:
            return json_response([{"id": 150, "identifier": "wyoung"}])
        return json_response([])

    async with make_client(handler) as client:
        assert await _resolve_member(client, "wyoung") == 150
        with pytest.raises(ResolutionError, match="No member"):
            await _resolve_member(client, "ghost")


async def test_resolve_contact_two_part_name_scoped_to_company():
    def handler(request):
        cond = request.url.params["conditions"]
        assert "company/id = 19332" in cond
        assert "firstName contains 'Jane'" in cond
        assert "lastName contains 'Doe'" in cond
        return json_response([{"id": 88, "firstName": "Jane", "lastName": "Doe"}])

    async with make_client(handler) as client:
        assert await _resolve_contact(client, "Jane Doe", 19332) == 88


async def test_resolve_contact_single_token_matches_either_name():
    def handler(request):
        cond = request.url.params["conditions"]
        assert "firstName contains 'Jane' or lastName contains 'Jane'" in cond
        return json_response([{"id": 88, "firstName": "Jane", "lastName": "Doe"}])

    async with make_client(handler) as client:
        assert await _resolve_contact(client, "Jane", 19332) == 88
