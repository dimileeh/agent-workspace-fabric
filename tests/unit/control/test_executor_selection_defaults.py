"""The executor freezes its configured native defaults once, including fallbacks."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentDefaults
from awf.common.agent_execution import execution_from_policy
from awf.common.commands import FakeCommandRunner
from awf.control.executor.helpers import _agent_defaults_for_workspace
from awf.db.enums import AgentRuntime
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _RecordingValidation,
    _seed_ready,
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


@pytest.mark.parametrize(
    ("explicit", "configured_model", "expected_model", "expected_provider"),
    [
        ({}, "openai/gpt-5", "openai/gpt-5", "openai"),
        ({"effort": "high"}, "openrouter/coder", "openrouter/coder", "openrouter"),
        ({"model": "anthropic/coder"}, "openai/gpt-5", "anthropic/coder", "anthropic"),
        ({"provider": "ollama"}, "ollama/coder:cloud", "ollama/coder:cloud", "ollama"),
        (
            {"provider": "ollama", "model": "ollama/explicit:cloud"},
            "openai/gpt-5",
            "ollama/explicit:cloud",
            "ollama",
        ),
    ],
)
async def test_opencode_default_freeze_keeps_provider_model_and_explicit_fields_coherent(
    factory, tmp_path, explicit, configured_model, expected_model, expected_provider
):
    from awf.control.executor.state_ops import _freeze_execution_defaults

    policy = freeze_execution_policy(
        request(agent="opencode", **explicit), profile={}, enabled=False, environ={}
    )
    async with factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="OpenCode defaults",
            task_prompt="fix",
            agent="opencode",
            test_commands=[],
            task_policy=policy,
        )
        await session.commit()
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    executor._config = replace(
        executor._config,
        agent_defaults={AgentRuntime.opencode: AgentDefaults(model=configured_model, effort="low")},
    )
    assert await _freeze_execution_defaults(executor, row)
    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        selected = execution_from_policy(persisted.task_policy)
    assert (selected.model, selected.provider) == (expected_model, expected_provider)
    assert selected.effort == explicit.get("effort", "low")
    assert selected.model_explicit == ("model" in explicit)
    assert selected.effort_explicit == ("effort" in explicit)
    executor._config = replace(
        executor._config, default_models={AgentRuntime.opencode: "changed/model"}
    )
    assert await _freeze_execution_defaults(executor, persisted)
    assert execution_from_policy(persisted.task_policy) == selected


async def test_opencode_explicit_provider_cannot_be_overridden_by_configured_model(
    factory, tmp_path
):
    from awf.control.executor.state_ops import _freeze_execution_defaults

    policy = freeze_execution_policy(
        request(agent="opencode", provider="ollama"), profile={}, enabled=False, environ={}
    )
    async with factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="OpenCode provider constraint",
            task_prompt="fix",
            agent="opencode",
            test_commands=[],
            task_policy=policy,
        )
        await session.commit()
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    executor._config = replace(
        executor._config, default_models={AgentRuntime.opencode: "openai/gpt-5"}
    )
    with pytest.raises(ValueError, match="AGENT_PROVIDER_UNSUPPORTED"):
        await _freeze_execution_defaults(executor, row)
    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        assert persisted.task_policy == policy
    assert row.task_policy == policy


@pytest.mark.parametrize("task_kind", ["sync_feature_pr", "sync_release_pr"])
@pytest.mark.parametrize("lost_claim", [False, True])
async def test_initial_sync_adapter_and_restarted_monitor_use_same_configured_defaults(
    factory, tmp_path, monkeypatch, task_kind, lost_claim
):
    from awf.control.executor import monitor_handoff, monitor_handoff_sync
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_015 import (
        _adoption_metadata,
    )

    policy = freeze_execution_policy(request(), profile={}, enabled=False, environ={})
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
        task_kind=task_kind,
        task_policy=policy,
        resolved_profile=WorkspaceProfile(name="sync-defaults").model_dump(mode="json"),
    )
    monkeypatch.setattr(monitor_handoff_sync, "count_commits_ahead", AsyncMock(return_value=1))
    monkeypatch.setattr(
        monitor_handoff_sync,
        "find_or_create_release_pr",
        AsyncMock(
            return_value=(_adoption_metadata(number=42, url=policy["pr_adoption"]["pr_url"]), False)
        ),
    )
    captured = []
    original = monitor_handoff.get_adapter

    def capture_adapter(agent, **kwargs):
        captured.append(kwargs["defaults"])
        return original(agent, **kwargs)

    monkeypatch.setattr(monitor_handoff, "get_adapter", capture_adapter)
    monitor = AsyncMock()
    executor = _make_executor(
        FakeCommandRunner(),
        factory,
        tmp_path,
        validation=_RecordingValidation(),
        pr_monitor_factory=lambda _adapter, **_: monitor,
    )
    executor._config = replace(
        executor._config,
        agent_defaults={AgentRuntime.codex: AgentDefaults(model="configured-sync", effort="low")},
        default_models=None,
    )
    if lost_claim:
        original_recheck = executor._recheck_status

        async def transfer_claim_after_recheck(workspace_id, **kwargs):
            result = await original_recheck(workspace_id, **kwargs)
            if kwargs.get("action") in {"sync_feature_pr_handoff", "sync_release_pr_monitor_build"}:
                async with factory() as session:
                    row = await WorkspaceRepository(session).get(workspace_id)
                    row.execution_claim_epoch += 1
                    await session.commit()
            return result

        monkeypatch.setattr(executor, "_recheck_status", transfer_claim_after_recheck)
    await executor.execute(workspace_id)
    if lost_claim:
        assert captured == []
        monitor.run.assert_not_awaited()
        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row.status == "running"
            assert row.task_policy["agent_execution_defaults_pending"] is True
        return
    assert len(captured) == 1
    assert (captured[0].model, captured[0].effort) == ("configured-sync", "low")
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "monitoring_pr"
        selected = execution_from_policy(row.task_policy)
        assert selected == captured[0].execution
        assert "agent_execution_defaults_pending" not in row.task_policy
        row.compose_file_path = str(tmp_path / "compose.yml")
        await session.commit()
    restarted = _make_executor(
        FakeCommandRunner(),
        factory,
        tmp_path,
        pr_monitor_factory=lambda _adapter, **_: monitor,
    )
    restarted._config = replace(
        restarted._config,
        default_models={AgentRuntime.codex: "changed-after-restart"},
    )
    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        monitor_handoff, "replay_setup_on_monitor_resume", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        monitor_handoff, "_refresh_selected_agent_material", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        restarted, "_restart_compose_runtime_for_monitor_resume", AsyncMock(return_value=True)
    )
    assert await restarted.resume_pr_monitor_handoff(workspace_id) is not None
    assert len(captured) == 2
    assert captured[1] == captured[0]
