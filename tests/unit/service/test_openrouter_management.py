"""OpenRouter key-management client against a fake management API (no live calls)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import structlog

from awf.service.openrouter_management import (
    OpenRouterManagementClient,
    OpenRouterManagementError,
)

_MGMT = "sk-or-mgmt-sentinel-0123456789"
_MINTED = "sk-or-v1-minted-sentinel-abcdef"


def _key(hash_: str = "h1", name: str = "awf-ws_x-1", **overrides):
    payload = {
        "hash": hash_,
        "name": name,
        "label": "sk-or-v1-abc...xyz",
        "disabled": False,
        "limit": 10.0,
        "limit_remaining": 7.5,
        "limit_reset": None,
        "include_byok_in_limit": True,
        "usage": 2.0,
        "usage_daily": 0.5,
        "byok_usage": 0.5,
        "created_at": "2026-11-10T00:00:00Z",
        "updated_at": None,
    }
    payload.update(overrides)
    return payload


def _client(handler) -> OpenRouterManagementClient:
    return OpenRouterManagementClient(_MGMT, transport=httpx.MockTransport(handler))


async def test_create_sends_name_limit_and_byok_flag_and_returns_the_key_once():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"data": _key(limit=3.25), "key": _MINTED})

    minted = await _client(handler).create_key("awf-ws_x-1", Decimal("3.2500009"))

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/keys"
    assert request.headers["Authorization"] == f"Bearer {_MGMT}"
    assert json.loads(request.content) == {
        "name": "awf-ws_x-1",
        "limit": 3.25,
        "include_byok_in_limit": True,
    }
    assert minted.key == _MINTED
    assert minted.info.hash == "h1"
    assert minted.info.limit == Decimal("3.25")
    assert _MINTED not in repr(minted)
    assert _MINTED not in str(minted)


async def test_get_update_and_delete_map_to_the_key_endpoints():
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(200, json={"data": _key(limit=12.0)})

    client = _client(handler)
    info = await client.get_key("h1")
    patched = await client.update_key_limit("h1", Decimal("12"))
    deleted = await client.delete_key("h1")

    assert info.spend == Decimal("2.5")
    assert info.usage == Decimal("2.0") and info.byok_usage == Decimal("0.5")
    assert patched.limit == Decimal("12.0")
    assert deleted is True
    assert calls[0][:2] == ("GET", "/api/v1/keys/h1")
    assert calls[1][:2] == ("PATCH", "/api/v1/keys/h1")
    assert json.loads(calls[1][2]) == {"limit": 12.0}
    assert calls[2][:2] == ("DELETE", "/api/v1/keys/h1")


async def test_delete_of_a_missing_key_counts_as_already_deleted():
    client = _client(lambda _request: httpx.Response(404, json={"error": "not found"}))

    assert await client.delete_key("gone") is False
    with pytest.raises(OpenRouterManagementError) as raised:
        await client.get_key("gone")
    assert raised.value.reason_code == "PROVIDER_MANAGED_KEY_NOT_FOUND"
    assert raised.value.http_status == 404
    assert raised.value.operation == "get"


async def test_exhaustion_is_derived_from_remaining_limit_or_spend():
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "remaining":
            return httpx.Response(200, json={"data": _key(limit_remaining=0)})
        if name == "spent":
            return httpx.Response(
                200, json={"data": _key(limit=2.5, limit_remaining=None, usage=2, byok_usage=0.5)}
            )
        return httpx.Response(200, json={"data": _key(limit=None, limit_remaining=None)})

    client = _client(handler)
    assert (await client.get_key("remaining")).exhausted is True
    assert (await client.get_key("spent")).exhausted is True
    assert (await client.get_key("unlimited")).exhausted is False


async def test_list_paginates_by_offset_until_a_short_page():
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(request.url.params["offset"])
        assert request.url.params["include_disabled"] == "true"
        size = 100 if offset < 200 else 7
        return httpx.Response(
            200, json={"data": [_key(f"h{offset + i}", f"k{offset + i}") for i in range(size)]}
        )

    listing = await _client(handler).list_keys()

    assert offsets == ["0", "100", "200"]
    assert len(listing.keys) == 207
    assert listing.truncated is False


async def test_list_stops_at_the_page_bound_and_reports_truncation():
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200, json={"data": [_key(f"h{offset + i}", f"k{offset + i}") for i in range(100)]}
        )

    listing = await _client(handler).list_keys(max_pages=2)

    assert len(listing.keys) == 200
    assert listing.truncated is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "PROVIDER_MANAGEMENT_API_UNAUTHORIZED"),
        (403, "PROVIDER_MANAGEMENT_API_UNAUTHORIZED"),
        (404, "PROVIDER_MANAGED_KEY_NOT_FOUND"),
        (429, "PROVIDER_MANAGEMENT_API_RATE_LIMITED"),
        (500, "PROVIDER_MANAGEMENT_API_UNAVAILABLE"),
        (503, "PROVIDER_MANAGEMENT_API_UNAVAILABLE"),
        (400, "PROVIDER_MANAGEMENT_API_REJECTED"),
        (409, "PROVIDER_MANAGEMENT_API_REJECTED"),
        (302, "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID"),
    ],
)
async def test_http_status_is_classified_without_echoing_secrets(status, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"location": "https://evil.example/steal"} if status == 302 else {},
            json={"error": {"message": f"bad {_MGMT} {_MINTED}"}},
        )

    with pytest.raises(OpenRouterManagementError) as raised:
        await _client(handler).create_key("awf-ws_x-1", Decimal("1"))

    assert raised.value.reason_code == reason
    assert raised.value.http_status == status
    assert raised.value.operation == "create"
    assert _MGMT not in str(raised.value) and _MINTED not in str(raised.value)
    assert _MGMT not in repr(raised.value)


async def test_redirects_are_never_followed():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://evil.example/keys"})

    with pytest.raises(OpenRouterManagementError):
        await _client(handler).get_key("h1")
    assert hosts == ["openrouter.ai"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"data": {"hash": "h1"}}),
        httpx.Response(200, json={"data": _key(usage="NaN")}),
        httpx.Response(200, json={"data": _key(usage=-1)}),
        httpx.Response(200, json={"data": _key(hash="")}),
        httpx.Response(200, json={"data": _key(name="x" * 600)}),
        httpx.Response(200, json={"data": _key(name=_MGMT)}),
        httpx.Response(200, json={"data": "not-a-key"}),
        httpx.Response(200, json={"data": _key(created_at=12)}),
        httpx.Response(200, json={"data": _key(usage=None)}),
        httpx.Response(200, json={"data": _key(hash="../escape")}),
        httpx.Response(
            200,
            content=json.dumps({"data": _key()}).replace('"usage": 2.0', '"usage": NaN').encode(),
        ),
        httpx.Response(200, content=b"{" + b" " * 2_100_000 + b"}"),
    ],
)
async def test_invalid_or_oversized_responses_are_rejected(response):
    with pytest.raises(OpenRouterManagementError) as raised:
        await _client(lambda _request: response).get_key("h1")
    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID"


async def test_create_requires_the_one_time_key_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": _key()})

    with pytest.raises(OpenRouterManagementError) as raised:
        await _client(handler).create_key("awf-ws_x-1", Decimal("1"))
    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID"


async def test_transport_failures_are_unavailable_and_never_logged_with_secrets():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {_MGMT}", request=request)

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(OpenRouterManagementError) as raised,
    ):
        await _client(handler).delete_key("h1")

    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_API_UNAVAILABLE"
    assert raised.value.http_status is None
    assert _MGMT not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert _MGMT not in json.dumps(logs, default=str)


def test_client_repr_and_missing_key_are_safe():
    assert _MGMT not in repr(OpenRouterManagementClient(_MGMT))
    with pytest.raises(ValueError, match="PROVIDER_MANAGEMENT_KEY_MISSING"):
        OpenRouterManagementClient("  ")


async def test_list_rejects_a_non_list_page():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"hash": "h1"}})

    with pytest.raises(OpenRouterManagementError) as raised:
        await _client(handler).list_keys()
    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID"
    assert raised.value.operation == "list"


async def test_unsafe_key_hashes_never_reach_a_request_path():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("request must not be sent")

    with pytest.raises(ValueError, match="PROVIDER_MANAGED_KEY_HASH_INVALID"):
        await _client(handler).get_key("../keys")
