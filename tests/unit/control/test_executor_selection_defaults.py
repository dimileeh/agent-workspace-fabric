"""The executor freezes its configured native defaults once, including fallbacks."""

from dataclasses import replace

import pytest

from awf.adapters.base import AgentDefaults
from awf.common.agent_execution import execution_from_policy
from awf.common.commands import FakeCommandRunner
from awf.control.executor.helpers import _agent_defaults_for_workspace
from awf.db.enums import AgentRuntime
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    factory,
)
from tests.unit.service.test_agent_execution_snapshot import freeze_execution_policy, request

_IMPORTED_FIXTURES = (factory,)


@pytest.mark.parametrize("model_override", [None, "legacy-custom"])
@pytest.mark.parametrize("explicit", ["neither", "model", "effort", "both"])
async def test_create_execute_restart_preserves_authoritative_defaults(
    factory, tmp_path, monkeypatch, model_override, explicit
):
    payload = request(
        **{
            k: v
            for k, v in {"model": "explicit-model", "effort": "high"}.items()
            if explicit in {k, "both"}
        },
        provider_recovery={"fallbacks": [{"agent": "claude_code", "model": "fallback-model"}]},
    )
    policy = freeze_execution_policy(payload, profile={}, enabled=False, environ={})
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="defaults",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            task_policy=policy,
        )
        ws.status = "ready"
        await session.commit()
        workspace_id = ws.id
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    executor._config = replace(
        executor._config,
        default_models={AgentRuntime.codex: model_override} if model_override else None,
        agent_defaults={
            AgentRuntime.codex: AgentDefaults(model="custom-model", effort="low"),
            AgentRuntime.claude_code: AgentDefaults(model="custom-claude", effort="low"),
        },
    )
    # Stop after adapter selection: no validation, pushing or repository work.
    captured = []

    def capture_adapter(agent, **kwargs):
        captured.append(kwargs["defaults"])
        raise RuntimeError("stop after selection")

    monkeypatch.setattr("awf.control.executor.execution_flow.get_adapter", capture_adapter)
    # Existing async gates remain intact; a real claim persists the selection.
    await executor.execute(workspace_id)
    expected_model = (
        "explicit-model" if explicit in {"model", "both"} else (model_override or "custom-model")
    )
    expected_effort = "high" if explicit in {"effort", "both"} else "low"
    assert captured[0].model == expected_model
    assert captured[0].effort == expected_effort
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        selected = execution_from_policy(row.task_policy)
        assert (selected.model, selected.effort) == (expected_model, expected_effort)
        fallback = execution_from_policy(row.task_policy["provider_recovery"]["fallbacks"][0])
        assert (fallback.model, fallback.effort) == ("fallback-model", "low")
        row.status = "ready"
        await session.commit()
    restarted = _make_executor(FakeCommandRunner(), factory, tmp_path)
    restarted._config = replace(
        restarted._config,
        agent_defaults={AgentRuntime.codex: AgentDefaults(model="changed", effort="xhigh")},
    )
    await restarted.execute(workspace_id)
    assert (captured[-1].model, captured[-1].effort) == (expected_model, expected_effort)
    assert (
        _agent_defaults_for_workspace(row, restarted._defaults_for(AgentRuntime.codex)).execution
        == selected
    )


@pytest.mark.parametrize("changed", ["missing", "status", "epoch", "monitor_owner"])
async def test_stale_executor_cannot_freeze_defaults(factory, tmp_path, changed):
    from awf.control.executor.state_ops import _freeze_execution_defaults

    policy = freeze_execution_policy(request(), profile={}, enabled=False, environ={})
    async with factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="defaults",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            task_policy=policy,
        )
        await session.commit()
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    async with factory() as session:
        current = await WorkspaceRepository(session).get(row.id)
        if changed == "missing":
            await session.delete(current)
        elif changed == "status":
            current.status = "ready"
        elif changed == "epoch":
            current.execution_claim_epoch += 1
        else:
            current.monitor_claimed_by = "new-owner"
        await session.commit()
    assert await _freeze_execution_defaults(executor, row) is False
    assert row.task_policy == policy


async def test_concurrent_default_freeze_reuses_winner_and_preserves_expanded_fallback(
    factory, tmp_path
):
    from awf.control.executor.state_ops import _freeze_execution_defaults

    payload = request(
        provider_recovery={
            "fallbacks": [{"agent": "codex", "provider": "ollama", "model": "coder"}]
        }
    )
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile={}, enabled=True, environ={})
    async with factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="defaults",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            task_policy=policy,
        )
        await session.commit()
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    contender = await executor._load_workspace(row.id)
    assert await _freeze_execution_defaults(executor, row)
    executor._config = replace(executor._config, default_models={AgentRuntime.codex: "loser"})
    assert await _freeze_execution_defaults(executor, contender)
    assert contender.task_policy == row.task_policy
    assert (
        contender.task_policy["provider_recovery"]["fallbacks"]
        == policy["provider_recovery"]["fallbacks"]
    )
