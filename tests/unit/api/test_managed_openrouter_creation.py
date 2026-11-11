"""Creation freezes managed OpenRouter selections and their budget beside them."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from awf.common.config import get_settings
from awf.db.models import Workspace
from awf.db.repositories import WorkerHeartbeatRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_MANAGEMENT = "sk-or-mgmt-creation-sentinel"
_PROFILE = {"name": "inline", "runtime": {"environment": {"TOOL_MODE": "safe"}}}


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _worker(engine, version: int = 3, worker_id: str = "capable") -> None:
    now = datetime.now(UTC)
    async with make_session_factory(engine)() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id=worker_id,
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=version,
        )
        await session.commit()


def _body(*, provider: str = "openrouter", model: str = "m/x", profile=True, **task):
    body = {
        "repo": {"url": "https://github.com/example/project"},
        "task": {"title": "t", "prompt": "fix", "provider": provider, "model": model, **task},
        "preflight": {"allow_unverified_model": True},
    }
    if profile:
        body["workspace"] = {"profile": _PROFILE}
    return body


async def _policy(engine, workspace_id: str) -> dict:
    async with make_session_factory(engine)() as session:
        return (await session.get(Workspace, workspace_id)).task_policy


async def test_default_budget_and_managed_reference_are_frozen(client, engine, managed):
    await _worker(engine)

    response = await client.post("/v1/workspaces", json=_body())

    assert response.status_code == 202, response.json()
    policy = await _policy(engine, response.json()["workspace_id"])
    assert policy["agent_execution"]["credential_ref"] == "managed/openrouter"
    assert policy["provider_budget"] == {
        "version": 1,
        "provider": "openrouter",
        "managed": True,
        "limit_usd": 10.0,
        "requested_usd": None,
        "source": "default",
    }
    assert _MANAGEMENT not in str(policy)


async def test_task_budget_override_is_frozen_and_replay_compares_it(client, engine, managed):
    await _worker(engine)
    headers = {"Idempotency-Key": "managed-budget"}

    created = await client.post(
        "/v1/workspaces", json=_body(provider_budget_usd=2.5), headers=headers
    )
    replay = await client.post(
        "/v1/workspaces", json=_body(provider_budget_usd=2.5), headers=headers
    )
    conflict = await client.post(
        "/v1/workspaces", json=_body(provider_budget_usd=3), headers=headers
    )

    assert created.status_code == 202, created.json()
    policy = await _policy(engine, created.json()["workspace_id"])
    assert policy["provider_budget"]["limit_usd"] == 2.5
    assert policy["provider_budget"]["requested_usd"] == 2.5
    assert policy["provider_budget"]["source"] == "task"
    assert replay.status_code == 202, replay.json()
    assert replay.json()["workspace_id"] == created.json()["workspace_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("value", [0, -1, 1_000_000, "abc", True])
async def test_invalid_budgets_are_rejected_by_the_schema(client, managed, value):
    response = await client.post("/v1/workspaces", json=_body(provider_budget_usd=value))
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("configured", "provider", "model"),
    [(False, "openrouter", "m/x"), (True, "ollama", "coder"), (True, None, None)],
)
async def test_a_budget_without_a_managed_selection_is_unsupported(
    client, engine, monkeypatch, configured, provider, model
):
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    if configured:
        monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT)
    else:
        monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    get_settings.cache_clear()
    await _worker(engine)

    response = await client.post(
        "/v1/workspaces", json=_body(provider=provider, model=model, provider_budget_usd=5)
    )

    get_settings.cache_clear()
    assert response.status_code == 409, response.json()
    assert (
        response.json()["detail"]["provider_readiness_preflight"]["reason_code"]
        == "PROVIDER_BUDGET_UNSUPPORTED"
    )


async def test_managed_admission_requires_every_live_worker_at_capability_three(
    client, engine, managed
):
    await _worker(engine, version=2)

    old = await client.post("/v1/workspaces", json=_body())

    assert (
        old.json()["detail"]["provider_readiness_preflight"]["reason_code"]
        == "EXECUTION_WORKER_CONTRACT_UNSUPPORTED"
    )
    await _worker(engine, version=3)
    await _worker(engine, version=3, worker_id="capable-2")
    accepted = await client.post("/v1/workspaces", json=_body())
    assert accepted.status_code == 202, accepted.json()


async def test_auto_profile_and_fallbacks_freeze_the_managed_decision(client, engine, managed):
    await _worker(engine)
    response = await client.post(
        "/v1/workspaces",
        json=_body(
            profile=False,
            provider="ollama",
            model="coder",
            provider_recovery={
                "fallbacks": [{"agent": "claude_code", "provider": "openrouter", "model": "m/y"}]
            },
        ),
    )

    assert response.status_code == 202, response.json()
    policy = await _policy(engine, response.json()["workspace_id"])
    assert policy["agent_execution_profile_pending"] is True
    assert policy["provider_budget"]["managed"] is True
    from awf.common.agent_execution import bind_execution_profile, execution_from_policy

    bound = bind_execution_profile(policy, agent="codex", profile=_PROFILE, environ={})
    assert execution_from_policy(bound).credential_ref is None
    fallback = execution_from_policy(bound["provider_recovery"]["fallbacks"][0])
    assert fallback.credential_ref == "managed/openrouter"


async def test_without_a_management_key_the_static_snapshot_is_unchanged(
    client, engine, monkeypatch
):
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    get_settings.cache_clear()
    await _worker(engine)

    response = await client.post("/v1/workspaces", json=_body())

    get_settings.cache_clear()
    assert response.status_code == 202, response.json()
    policy = await _policy(engine, response.json()["workspace_id"])
    assert "provider_budget" not in policy
    assert policy["agent_execution"]["credential_ref"] == "env/OPENROUTER_API_KEY"


async def test_hosted_requests_cannot_select_a_managed_key(managed):
    from awf.api.schemas import WorkspaceCreateRequest
    from awf.service.workspaces_create_policy import freeze_execution_policy

    payload = WorkspaceCreateRequest.model_validate(_body())
    with pytest.raises(ValueError, match="PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED"):
        freeze_execution_policy(
            payload,
            profile=_PROFILE,
            enabled=True,
            environ={},
            settings=SimpleNamespace(
                openrouter_management_key=_MANAGEMENT, openrouter_workspace_limit_usd=10.0
            ),
            hosted=True,
        )


async def test_connection_tests_freeze_a_fixed_diagnostic_budget(client, engine, managed):
    await _worker(engine)

    response = await client.post(
        "/v1/providers/connection-tests",
        json={"agent": "codex", "provider": "openrouter", "model": "m/x"},
        headers={"Idempotency-Key": "managed-diagnostic"},
    )

    assert response.status_code in {200, 202}, response.json()
    policy = await _policy(engine, response.json()["workspace_id"])
    assert policy["agent_execution"]["credential_ref"] == "managed/openrouter"
    assert policy["provider_budget"]["limit_usd"] == 1.0
    assert policy["provider_budget"]["source"] == "diagnostic"
    environment = policy["provider_diagnostic"]["request"]["profile"]["runtime"]["environment"]
    assert "OPENROUTER_API_KEY" not in environment
