"""Controller role separation and stable physical facts during busy transitions."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.api.deps import require_api_token
from awf.common.config import Settings, get_settings
from awf.hosted_node.host_linux import LinuxSnapshot
from awf.hosted_node.host_runtime import HostRuntime
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.mark.parametrize("token", [None, "wrong", "operator"])
def test_direct_operator_dependency_preserves_legacy_request_none(token):
    settings = Settings(_env_file=None, api_token="operator")
    credentials = (
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
    )
    if token == "operator":
        require_api_token(credentials, settings)
    else:
        with pytest.raises(HTTPException) as denial:
            require_api_token(credentials, settings)
        assert denial.value.status_code == 401


def test_direct_operator_dependency_without_config_remains_closed():
    with pytest.raises(HTTPException) as denial:
        require_api_token(settings=Settings(_env_file=None, api_token=None))
    assert denial.value.status_code == 503


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("method", "path", "operator_status"),
    [
        ("GET", "/v1/workspaces", 200),
        ("GET", "/v1/workspaces/missing", 404),
        ("POST", "/v1/workspaces/missing/cancel", 404),
        ("POST", "/v1/workspaces/missing/stop", 404),
        ("DELETE", "/v1/workspaces/missing", 404),
    ],
)
async def test_controller_cannot_bypass_ordinary_operator_dependency(
    authority, enabled, method, path, operator_status
):
    runtime, _, factory = authority
    runtime.receiver.config = runtime.receiver.config.model_copy(
        update={"runtime_enabled": enabled}
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: settings
    headers = {
        **runtime.receiver.identity.headers(),
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "control-test",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method, path, headers=headers, json={} if method == "POST" else None
        )
        assert response.status_code == 401, response.text
        headers["Authorization"] = "Bearer synthetic-operator"
        response = await client.request(
            method, path, headers=headers, json={} if method == "POST" else None
        )
        assert response.status_code == operator_status, response.text


@pytest.mark.parametrize("before_idle", [False, True])
@pytest.mark.parametrize("empty_changes", [False, True])
async def test_busy_host_transition_preserves_health_not_quiescence(
    authority, monkeypatch, before_idle, empty_changes
):
    runtime, _, factory = authority
    docker = Mock(quiescent=AsyncMock(return_value=True), healthy=AsyncMock(return_value=True))
    host = HostRuntime(runtime.receiver.config, factory, runtime.receiver.identity, docker)
    before = LinuxSnapshot("boot", 1, 2, 3, True, before_idle)
    after = replace(before, execution_quiescent=not before_idle, workspaces_empty=not empty_changes)
    host.linux.sample = Mock(side_effect=[before, after])
    monkeypatch.setattr("awf.hosted_node.host_runtime.validate_database", AsyncMock())
    observation = await host.observe(runtime.receiver.identity)
    assert observation is not None
    assert observation.retained_state_verified and not observation.quiescent


@pytest.mark.parametrize("field", ["boot_id", "device", "state_inode", "cgroup_inode"])
async def test_busy_host_transition_never_ignores_changed_physical_facts(
    authority, monkeypatch, field
):
    runtime, _, factory = authority
    docker = Mock(quiescent=AsyncMock(return_value=False), healthy=AsyncMock(return_value=True))
    host = HostRuntime(runtime.receiver.config, factory, runtime.receiver.identity, docker)
    before = LinuxSnapshot("boot", 1, 2, 3, False, False)
    after = replace(before, **{field: "other" if field == "boot_id" else 999})
    host.linux.sample = Mock(side_effect=[before, after])
    monkeypatch.setattr("awf.hosted_node.host_runtime.validate_database", AsyncMock())
    assert await host.observe(runtime.receiver.identity) is None
