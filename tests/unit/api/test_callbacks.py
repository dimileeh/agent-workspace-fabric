"""External callback registration API contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api import schemas as api_schemas
from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.session import make_session_factory

_VALID_BODY = {
    "name": "operator-console",
    "target_url": "https://operator.example.com/awf/events",
    "event_types": ["workspace.*", "merge.*", "operation.*"],
}


class _NoLegacyIPv4Labels:
    def split(self, separator: str) -> list[str]:
        assert separator == "."
        return []


async def _subscription_count(engine: AsyncEngine) -> int:
    from awf.db.models import CallbackSubscription

    factory = make_session_factory(engine)
    async with factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(CallbackSubscription))
            or 0
        )


@pytest.fixture
async def callback_app_and_client(engine: AsyncEngine) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield app, c


@pytest.mark.unit
async def test_register_callback_requires_idempotency_key(client: AsyncClient) -> None:
    response = await client.post("/v1/callbacks", json=_VALID_BODY)

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
async def test_register_callback_rejects_oversized_idempotency_key(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers={"Idempotency-Key": "k" * 129},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header must be at most 128 characters.",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_persists_safe_public_contract(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers={"Idempotency-Key": "callback-register-1"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("cb_")
    assert body["name"] == "operator-console"
    assert body["target_url"] == "https://operator.example.com/awf/events"
    assert body["event_types"] == ["workspace.*", "merge.*", "operation.*"]
    assert body["enabled"] is True
    assert body["timeout_seconds"] == 10
    assert body["max_attempts"] == 3
    assert body["initial_backoff_seconds"] == 5
    assert body["disabled_at"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None
    assert "secret" not in body
    assert "headers" not in body
    assert "authorization" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {**_VALID_BODY, "target_url": "ftp://operator.example.com/events"},
        {**_VALID_BODY, "target_url": "https://user:pass@operator.example.com/events"},
        {**_VALID_BODY, "target_url": "https://operator.example.com/events#frag"},
        {**_VALID_BODY, "target_url": "https:///missing-host"},
        {**_VALID_BODY, "event_types": []},
        {**_VALID_BODY, "event_types": [""]},
        {**_VALID_BODY, "event_types": ["system.secret"]},
        {**_VALID_BODY, "headers": {"Authorization": "Bearer should-not-be-accepted"}},
    ],
)
async def test_register_callback_validates_url_events_and_extra_fields(
    client: AsyncClient,
    payload: dict[str, object],
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=payload,
        headers={"Idempotency-Key": "callback-invalid"},
    )

    assert response.status_code == 422


@pytest.mark.unit
async def test_callbacks_endpoints_return_unavailable_when_disabled(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        callbacks_enabled=False,
    )

    register_response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers={"Idempotency-Key": "callback-disabled"},
    )
    list_response = await client.get("/v1/callbacks")

    assert register_response.status_code == 503
    assert register_response.json()["detail"] == {
        "error_code": "CALLBACKS_DISABLED",
        "message": "External callbacks are disabled by configuration.",
    }
    assert list_response.status_code == 503
    assert list_response.json()["detail"] == {
        "error_code": "CALLBACKS_DISABLED",
        "message": "External callbacks are disabled by configuration.",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_url",
    [
        "http://localhost/events",
        "http://localhost./events",
        "http://awf.localhost/events",
        "http://operator.local/events",
        "http://operator.localdomain/events",
        "http://internal/events",
        "http://127.0.0.1/events",
        "http://127.1/events",
        "http://0177.0.0.1/events",
        "http://0x7f000001/events",
        "http://2130706433/events",
        "http://10.0.0.5/events",
        "http://172.16.0.5/events",
        "http://192.168.0.5/events",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/events",
        "http://[::ffff:127.0.0.1]/events",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
        "http://[fe80::1]/events",
    ],
)
async def test_register_callback_rejects_internal_target_hosts_without_insert(
    client: AsyncClient,
    engine: AsyncEngine,
    target_url: str,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "target_url": target_url},
        headers={"Idempotency-Key": f"callback-internal-{target_url}"},
    )

    assert response.status_code == 422
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_url",
    [
        "http://[::ffff:127.0.0.1]/events",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
    ],
)
def test_callback_target_rejects_ipv4_mapped_ipv6_when_runtime_marks_global(
    monkeypatch: pytest.MonkeyPatch,
    target_url: str,
) -> None:
    real_ip_address = api_schemas.ipaddress.ip_address

    class LegacyIPv4MappedAddress:
        is_global = True
        is_multicast = False

        def __init__(self, ipv4_mapped: object) -> None:
            self.ipv4_mapped = ipv4_mapped

    def legacy_ip_address(value: str) -> object:
        address = real_ip_address(value)
        ipv4_mapped = getattr(address, "ipv4_mapped", None)
        if ipv4_mapped is None:
            return address
        return LegacyIPv4MappedAddress(ipv4_mapped)

    monkeypatch.setattr(api_schemas.ipaddress, "ip_address", legacy_ip_address)

    with pytest.raises(ValidationError, match="target_url must use a public host"):
        api_schemas.CallbackSubscriptionCreateRequest.model_validate(
            {**_VALID_BODY, "target_url": target_url}
        )


@pytest.mark.unit
def test_legacy_ipv4_literal_detector_rejects_malformed_legacy_hosts() -> None:
    assert api_schemas._looks_like_legacy_ipv4_literal(  # type: ignore[arg-type]
        _NoLegacyIPv4Labels()
    ) is False
    assert api_schemas._looks_like_legacy_ipv4_literal("192.168..1") is False
    assert api_schemas._looks_like_legacy_ipv4_literal("0xg.168.1.1") is False
    assert api_schemas._looks_like_legacy_ipv4_literal("0x7f.0.0.1") is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "event_type",
    [
        "workspace.internal_secret",
        "merge.internal_secret",
        "operation.internal_secret",
    ],
)
async def test_register_callback_rejects_internal_namespaced_event_types_without_insert(
    client: AsyncClient,
    engine: AsyncEngine,
    event_type: str,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "event_types": [event_type]},
        headers={"Idempotency-Key": f"callback-invalid-{event_type}"},
    )

    assert response.status_code == 422
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_accepts_exact_public_event_types(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "event_types": [
                "operation.state_changed",
                "workspace.state_changed",
                "merge.candidate_updated",
                "workspace.state_changed",
            ],
        },
        headers={"Idempotency-Key": "callback-public-exact"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["event_types"] == [
        "operation.state_changed",
        "workspace.state_changed",
        "merge.candidate_updated",
    ]
    assert "secret" not in body
    assert "headers" not in body
    assert "authorization" not in body


@pytest.mark.unit
async def test_register_callback_idempotent_replay_returns_original_without_duplicate_rows(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    headers = {"Idempotency-Key": "callback-replay"}

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    before_count = await _subscription_count(engine)
    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    after_count = await _subscription_count(engine)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert after_count == before_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed_body",
    [
        {**_VALID_BODY, "target_url": "https://operator.example.com/changed"},
        {**_VALID_BODY, "event_types": ["workspace.*", "operation.*"]},
        {**_VALID_BODY, "enabled": False},
    ],
)
async def test_register_callback_same_key_with_changed_body_returns_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    changed_body: dict[str, object],
) -> None:
    headers = {"Idempotency-Key": "callback-conflict"}

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    before_count = await _subscription_count(engine)
    conflict = await client.post(
        "/v1/callbacks",
        json=changed_body,
        headers=headers,
    )
    after_count = await _subscription_count(engine)

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_count == before_count == 1


@pytest.mark.unit
async def test_list_callbacks_returns_pagination_envelope_and_enabled_filter(
    client: AsyncClient,
) -> None:
    enabled = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "enabled"},
        headers={"Idempotency-Key": "callback-list-enabled"},
    )
    disabled = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "disabled",
            "target_url": "https://operator.example.com/disabled",
            "enabled": False,
        },
        headers={"Idempotency-Key": "callback-list-disabled"},
    )
    assert enabled.status_code == 201
    assert disabled.status_code == 201

    all_response = await client.get("/v1/callbacks")
    enabled_response = await client.get("/v1/callbacks", params={"enabled": True})

    assert all_response.status_code == 200
    all_body = all_response.json()
    assert all_body["next_cursor"] is None
    assert all_body["has_more"] is False
    assert all_body["limit"] == 50
    assert all_body["cursor"] is None
    assert {item["name"] for item in all_body["items"]} == {"enabled", "disabled"}

    assert enabled_response.status_code == 200
    assert [item["name"] for item in enabled_response.json()["items"]] == ["enabled"]


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, 501])
async def test_list_callbacks_validates_limit_bounds(
    client: AsyncClient,
    limit: int,
) -> None:
    response = await client.get("/v1/callbacks", params={"limit": limit})

    assert response.status_code == 422
