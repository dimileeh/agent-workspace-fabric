"""Initial native launches enforce the actual executor-default circuit."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.adapters.base import AgentDefaults
from awf.common.agent_execution import execution_from_policy, resolve_execution
from awf.common.commands import FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.db.models import WorkspaceEvent
from awf.db.repositories import ProviderModelCircuitBreakerRepository, WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.service.provider_recovery import provider_cooldown_not_before
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _RecordingValidation,
    _seed_ready,
    factory,
)
from tests.unit.service.test_agent_execution_snapshot import freeze_execution_policy, request

_IMPORTED_FIXTURES = (factory,)


@pytest.mark.parametrize("agent", ["codex", "claude_code", "opencode"])
@pytest.mark.parametrize("task_kind", ["feature_branch_pr", "sync_feature_pr", "sync_release_pr"])
@pytest.mark.parametrize("scenario", ["historical-open", "configured-open", "historical-only"])
async def test_first_launch_checks_actual_default_circuit(
    factory, tmp_path, monkeypatch, agent, task_kind, scenario
):
    from awf.control.executor import execution_flow, monitor_handoff, monitor_handoff_sync
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_015 import (
        _adoption_metadata,
    )

    historical = resolve_execution(agent=agent)
    configured = historical.model if scenario == "historical-open" else "configured-model"
    if agent == "opencode" and scenario != "historical-open":
        configured = "openai/configured-model"
    actual = resolve_execution(agent=agent, model=configured)
    policy = freeze_execution_policy(request(agent=agent), profile={}, enabled=False, environ={})
    recovery_state = {"retry_attempt_number": 1, "failure_fingerprints": ["earlier-failure"]}
    policy["provider_recovery_state"] = recovery_state
    policy["pr_adoption"] = {
        "repo_slug": "x/y",
        "pr_number": 42,
        "pr_url": "https://github.com/x/y/pull/42",
        "head_ref": "feature/existing",
        "base_ref": "development",
        "head_sha": "h" * 40,
        "base_sha": "b" * 40,
    }
    workspace_id = await _seed_ready(
        factory,
        agent=agent,
        task_kind=task_kind,
        task_policy=policy,
        resolved_profile=WorkspaceProfile(name="default-circuit").model_dump(mode="json"),
    )
    async with factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider=historical.provider if scenario == "historical-only" else actual.provider,
            model=historical.model if scenario == "historical-only" else configured,
            reason_code="PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity",
            workspace_id=None,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=300,
        )
        deadline = breaker.cooldown_until
        await session.commit()
    monkeypatch.setattr(monitor_handoff_sync, "count_commits_ahead", AsyncMock(return_value=1))
    monkeypatch.setattr(
        monitor_handoff_sync,
        "find_or_create_release_pr",
        AsyncMock(
            return_value=(_adoption_metadata(number=42, url=policy["pr_adoption"]["pr_url"]), False)
        ),
    )
    captured = []

    def stop_before_invocation(agent, **kwargs):
        captured.append(kwargs["defaults"].execution)
        raise RuntimeError("stop before paid invocation")

    monkeypatch.setattr(execution_flow, "get_adapter", stop_before_invocation)
    monkeypatch.setattr(monitor_handoff, "get_adapter", stop_before_invocation)
    monitor = AsyncMock()
    executor = _make_executor(
        FakeCommandRunner(),
        factory,
        tmp_path,
        validation=_RecordingValidation(),
        pr_monitor_factory=lambda *_args, **_kwargs: monitor,
    )
    executor._config = replace(
        executor._config,
        default_models=None,
        agent_defaults={AgentRuntime(agent): AgentDefaults(model=configured, effort="low")},
    )
    await executor.execute(workspace_id)
    monitor.run.assert_not_awaited()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        selected = execution_from_policy(row.task_policy)
        assert (selected.provider, selected.model) == (actual.provider, configured)
        assert "agent_execution_defaults_pending" not in row.task_policy
        if scenario == "historical-only":
            assert captured == [selected]
            assert row.task_policy["provider_recovery_state"] == recovery_state
            return
        assert captured == []
        assert row.status == "recovering"
        assert provider_cooldown_not_before(row.task_policy) == deadline
        state = row.task_policy["provider_recovery_state"]
        assert state["decision_reason_code"] == "PROVIDER_MODEL_CIRCUIT_OPEN"
        assert state["retry_attempt_number"] == 1
        assert state["failure_fingerprints"] == ["earlier-failure"]
        assert row.failure_reason is None
        # The existing resume query must wait until the actual circuit deadline.
        repo = WorkspaceRepository(session)
        assert await repo.list_resumable_recovering_ids(limit=1, now=datetime.now(UTC)) == []
        assert await repo.list_resumable_recovering_ids(
            limit=1, now=deadline + timedelta(seconds=1)
        ) == [workspace_id]


@pytest.mark.parametrize(
    "changed", ["missing", "status", "epoch", "owner", "monitor", "policy", "agent"]
)
async def test_launch_guard_fences_stale_snapshot_without_deferring_new_owner(
    factory, tmp_path, changed
):
    from awf.control.executor.state_ops import (
        _execution_circuit_allows_launch,
        _freeze_execution_defaults,
    )

    policy = freeze_execution_policy(request(), profile={}, enabled=False, environ={})
    workspace_id = await _seed_ready(factory, task_policy=policy)
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    snapshot = await executor._claim_ready(workspace_id, execution_owner_id="old")
    assert await _freeze_execution_defaults(executor, snapshot)
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        if changed == "missing":
            await session.delete(row)
        elif changed == "status":
            row.status = "cancelled"
        elif changed == "epoch":
            row.execution_claim_epoch += 1
        elif changed == "owner":
            row.execution_claimed_by = "new"
        elif changed == "monitor":
            row.monitor_claimed_by = "new"
        elif changed == "policy":
            row.task_policy = {**row.task_policy, "agent_model": "new-selection"}
        else:
            row.agent = "claude_code"
        await session.commit()
        expected = (row.status, row.version, row.task_policy, row.execution_claimed_by)
    assert await _execution_circuit_allows_launch(executor, snapshot) is False
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        if changed == "missing":
            assert row is None
        else:
            assert (row.status, row.version, row.task_policy, row.execution_claimed_by) == expected


@pytest.mark.parametrize("scenario", ["legacy-unset", "expired", "indefinite", "scoped"])
async def test_launch_guard_handles_legacy_expiry_and_connection_scopes(
    factory, tmp_path, scenario
):
    from awf.common.agent_execution import circuit_key
    from awf.control.executor.state_ops import _execution_circuit_allows_launch

    if scenario == "legacy-unset":
        policy = {}
    elif scenario == "scoped":
        selection = resolve_execution(
            agent="codex",
            provider="ollama",
            model="coder",
            allow_unverified=True,
            profile={
                "runtime": {"environment": {"OLLAMA_HOST": "ollama"}},
                "services": {"ollama": {}},
            },
        )
        policy = {"agent_execution": selection.model_dump()}
    else:
        policy = {"agent_execution": resolve_execution(agent="codex").model_dump()}
    workspace_id = await _seed_ready(factory, task_policy=policy)
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    snapshot = await executor._claim_ready(workspace_id)
    if scenario == "legacy-unset":
        assert await _execution_circuit_allows_launch(executor, snapshot)
        assert (await executor._load_workspace(workspace_id)).status == "running"
        return
    key = circuit_key(snapshot.agent, snapshot.task_policy, workspace_id=workspace_id)
    async with factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider=key[0],
            model=key[1],
            connection_scope=key[2] if len(key) == 3 else "legacy",
            reason_code="PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity",
            workspace_id=None,
            attempt_id=None,
            now=datetime.now(UTC) - timedelta(seconds=600)
            if scenario == "expired"
            else datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=300,
        )
        if scenario == "indefinite":
            breaker.cooldown_until = None
        await session.commit()
    if scenario == "scoped":
        independent_id = await _seed_ready(factory, task_policy=policy)
        independent = await executor._claim_ready(independent_id)
        results = await asyncio.gather(
            _execution_circuit_allows_launch(executor, snapshot),
            _execution_circuit_allows_launch(executor, snapshot),
            _execution_circuit_allows_launch(executor, independent),
        )
        assert results == [False, False, True]
    else:
        assert await _execution_circuit_allows_launch(executor, snapshot) == (scenario == "expired")
    async with factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).get(
            provider=key[0], model=key[1], connection_scope=key[2] if len(key) == 3 else "legacy"
        )
        row = await WorkspaceRepository(session).get(workspace_id)
        if scenario == "expired":
            assert (breaker.state, breaker.failure_count, row.status) == ("closed", 0, "running")
        else:
            assert (breaker.state, breaker.failure_count, row.status) == ("open", 1, "recovering")
            assert provider_cooldown_not_before(row.task_policy) is not None
            events = list(
                await session.scalars(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.reason_code == "PROVIDER_MODEL_CIRCUIT_OPEN",
                    )
                )
            )
            assert len(events) == 1
            assert events[0].payload["connection_scope"] == (key[2] if len(key) == 3 else "legacy")


async def test_default_circuit_deferral_resumes_frozen_identity_without_spending_retries(
    factory, tmp_path, monkeypatch
):
    from awf.control.executor.state_ops import (
        _execution_circuit_allows_launch,
        _freeze_execution_defaults,
    )
    from awf.db.enums import WorkspaceStatus

    policy = freeze_execution_policy(request(), profile={}, enabled=False, environ={})
    workspace_id = await _seed_ready(factory, task_policy=policy)
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    snapshot = await executor._claim_ready(workspace_id)
    assert await _freeze_execution_defaults(executor, snapshot)
    selected = execution_from_policy(snapshot.task_policy)
    async with factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider=selected.provider,
            model=selected.model,
            reason_code="PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity",
            workspace_id=None,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=300,
        )
        await session.commit()
    assert not await _execution_circuit_allows_launch(executor, snapshot)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        row = await repo.get(workspace_id)
        assert "retry_attempt_number" not in row.task_policy["provider_recovery_state"]
        # Simulate the worker claiming the row once cooldown has elapsed.
        breaker = await ProviderModelCircuitBreakerRepository(session).get(
            provider=selected.provider, model=selected.model
        )
        breaker.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        await repo.transition(row, to=WorkspaceStatus.running, reason_code="COOLDOWN_ELAPSED")
        await session.commit()
    captured = []

    def stop_before_invocation(agent, **kwargs):
        captured.append(kwargs["defaults"].execution)
        raise RuntimeError("stop before paid invocation")

    monkeypatch.setattr("awf.control.executor.execution_flow.get_adapter", stop_before_invocation)
    executor._config = replace(
        executor._config, default_models={AgentRuntime.codex: "changed-after-deferral"}
    )
    await executor.resume_recovering_execution(workspace_id)
    assert captured == [selected]
