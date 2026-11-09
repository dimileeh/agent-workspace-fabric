"""Existing creation surfaces expose selection while foundation admission fails closed."""

import pytest

from awf.api.schemas import WorkspaceCreateRequest

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


async def test_rest_rejects_disabled_third_party_even_with_all_overrides(client):
    response = await client.post(
        "/v1/workspaces",
        json={
            "repo": {"url": "https://github.com/example/project"},
            "task": {
                "title": "test",
                "prompt": "fix",
                "agent": "codex",
                "provider": "ollama",
                "model": "coder",
            },
            "preflight": {"allow_unverified_model": True, "provider_readiness_override": True},
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
    assert (
        body["detail"]["provider_readiness_preflight"]["reason_code"]
        == "THIRD_PARTY_AGENT_PROVIDER_ADMISSION_DISABLED"
    )


def test_schema_retains_provider_and_default_false_opt_in():
    payload = WorkspaceCreateRequest.model_validate(
        {
            "repo": {"url": "https://github.com/example/project"},
            "task": {
                "title": "task",
                "prompt": "fix",
                "provider": "openrouter",
                "model": "anthropic/test",
            },
        }
    )
    assert payload.task.provider == "openrouter"
    assert payload.preflight.allow_unverified_model is False


async def test_admission_rejects_unsupported_fleet_and_never_bypasses_runtime_network(
    client, engine, monkeypatch
):
    from datetime import UTC, datetime

    from awf.common.config import get_settings
    from awf.db.repositories import WorkerHeartbeatRepository
    from awf.db.session import make_session_factory

    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    get_settings.cache_clear()
    body = {
        "repo": {"url": "https://github.com/example/project"},
        "task": {"title": "test", "prompt": "fix", "provider": "ollama", "model": "coder"},
        "preflight": {"allow_unverified_model": True, "provider_readiness_override": True},
    }
    empty = await client.post("/v1/workspaces", json=body)
    assert (
        empty.json()["detail"]["provider_readiness_preflight"]["reason_code"]
        == "EXECUTION_WORKER_CONTRACT_UNSUPPORTED"
    )
    factory = make_session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="capable",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=1,
        )
        await session.commit()
    capable = await client.post("/v1/workspaces", json=body)
    assert (
        capable.json()["detail"]["provider_readiness_preflight"]["reason_code"]
        == "EXECUTION_NETWORK_PROBE_UNSUPPORTED"
    )
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="old",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
        )
        await session.commit()
    mixed = await client.post("/v1/workspaces", json=body)
    assert (
        mixed.json()["detail"]["provider_readiness_preflight"]["reason_code"]
        == "EXECUTION_WORKER_CONTRACT_UNSUPPORTED"
    )
