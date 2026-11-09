"""A fallback promotes its complete connection, never source credentials/effort."""

from datetime import UTC, datetime

from awf.common.agent_execution import execution_from_policy, resolve_execution
from awf.service.provider_recovery import decide_provider_recovery, install_recovery_selection
from awf.service.workspaces_retry_recovery import _prune_and_migrate_retired_agent


def test_full_fallback_selection_promotion(monkeypatch):
    from awf.common.config import get_settings

    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    get_settings.cache_clear()
    source = resolve_execution(agent="codex")
    target = resolve_execution(
        agent="claude_code", provider="openrouter", model="anthropic/test", allow_unverified=True
    )
    fallback = {
        "agent": target.agent,
        "provider": target.provider,
        "model": target.model,
        "agent_execution": target.model_dump(),
    }
    policy = {
        "agent_execution": source.model_dump(),
        "agent_model": source.model,
        "agent_effort": "xhigh",
        "cursor_auto_mode": "cost",
        "provider_recovery": {"fallbacks": [fallback], "max_same_provider_retries": 0},
    }
    decision = decide_provider_recovery(
        {
            "retryable": True,
            "provider": "openai",
            "model": source.model,
            "reason_code": "AGENT_PROVIDER_RATE_LIMITED",
        },
        task_policy=policy,
        current_agent="codex",
        current_model=source.model,
        now=datetime.now(UTC),
    )
    promoted = install_recovery_selection(policy, decision)
    assert execution_from_policy(promoted) == target
    assert "agent_effort" not in promoted
    assert "cursor_auto_mode" not in promoted
    retried, agent = _prune_and_migrate_retired_agent(policy.copy(), current_agent="gemini")
    assert agent == "claude_code"
    assert execution_from_policy(retried) == target
    assert "agent_effort" not in retried
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "false")
    get_settings.cache_clear()
    import pytest

    with pytest.raises(ValueError, match="ADMISSION_DISABLED"):
        install_recovery_selection(policy, decision)
    with pytest.raises(ValueError, match="ADMISSION_DISABLED"):
        _prune_and_migrate_retired_agent(policy.copy(), current_agent="gemini")
    get_settings.cache_clear()


def test_same_choice_retry_keeps_frozen_connection_and_no_implicit_native_fallback():
    selected = resolve_execution(
        agent="codex", provider="ollama", model="qwen3-coder", allow_unverified=True
    )
    policy = {"agent_execution": selected.model_dump(), "agent_model": selected.model}
    metadata = {
        "retryable": True,
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "provider": "openai",
        "model": "wrong",
    }
    decision = decide_provider_recovery(
        metadata,
        task_policy=policy,
        current_agent="codex",
        current_model=selected.model,
        now=datetime.now(UTC),
    )
    assert decision.action == "retry"
    assert decision.target_provider == "ollama" and decision.target_model == selected.model
    assert install_recovery_selection(policy, decision) == policy
    policy["provider_recovery"] = {"max_same_provider_retries": 0}
    assert (
        decide_provider_recovery(
            metadata,
            task_policy=policy,
            current_agent="codex",
            current_model=selected.model,
            now=datetime.now(UTC),
        ).action
        == "terminal"
    )


async def test_monitor_cooldown_uses_selected_connection_after_restart():
    from types import SimpleNamespace

    from awf.common.agent_execution import circuit_key
    from awf.db.repositories import ProviderModelCircuitBreakerRepository, WorkspaceRepository
    from awf.db.session import make_session_factory
    from awf.runtime.pr_monitor_runner.provider_ops import _provider_recovery_suppresses_cli
    from tests.postgres import postgres_test_engine

    selected = resolve_execution(
        agent="codex",
        provider="ollama",
        model="coder",
        allow_unverified=True,
        profile={
            "runtime": {"environment": {"OLLAMA_HOST": "ollama"}},
            "services": [{"name": "ollama"}],
        },
    )
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = await WorkspaceRepository(session).create(
                repo_url="https://example.com/repo",
                branch_base="development",
                task_title="task",
                task_prompt="fix",
                test_commands=[],
                agent="codex",
                task_policy={"agent_execution": selected.model_dump()},
            )
            await session.flush()
            workspace_id = row.id
            key = circuit_key("codex", row.task_policy, workspace_id=row.id)
            await ProviderModelCircuitBreakerRepository(session).record_failure(
                provider=key[0],
                model=key[1],
                connection_scope=key[2],
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                failure_fingerprint="f",
                workspace_id=row.id,
                attempt_id=None,
                now=datetime.now(UTC),
                failure_threshold=1,
                cooldown_seconds=600,
            )
            await session.commit()
        monitor = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))
        assert await _provider_recovery_suppresses_cli(monitor, workspace_id) is True
        async with factory() as session:
            reloaded = await WorkspaceRepository(session).get(workspace_id)
            assert execution_from_policy(reloaded.task_policy) == selected


def test_recovery_decision_exposes_complete_selected_target():
    target = resolve_execution(
        agent="claude_code",
        provider="ollama",
        model="coder",
        allow_unverified=True,
        environ={"AWF_OLLAMA_BASE_URL": "http://remote:11434"},
    )
    policy = {
        "provider_recovery": {
            "max_same_provider_retries": 0,
            "fallbacks": [
                {
                    "agent": target.agent,
                    "provider": target.provider,
                    "model": target.model,
                    "agent_execution": target.model_dump(),
                }
            ],
        }
    }
    decision = decide_provider_recovery(
        {"retryable": True},
        task_policy=policy,
        current_agent="codex",
        current_model="native",
        now=datetime.now(UTC),
    )
    assert decision.target_execution == target


async def test_persisted_monitor_and_retry_fallback_promote_entire_snapshot(monkeypatch):
    from awf.common.config import get_settings
    from awf.db.repositories import WorkspaceRepository
    from awf.db.session import make_session_factory
    from awf.service.provider_recovery import create_provider_recovery_attempt_row
    from tests.postgres import postgres_test_engine

    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    get_settings.cache_clear()
    target = resolve_execution(
        agent="claude_code", provider="openrouter", model="anthropic/test", allow_unverified=True
    )
    source = resolve_execution(agent="codex")
    policy = {
        "agent_execution": source.model_dump(),
        "agent_model": source.model,
        "agent_effort": "xhigh",
        "provider_recovery": {
            "max_same_provider_retries": 0,
            "fallbacks": [
                {
                    "agent": target.agent,
                    "provider": target.provider,
                    "model": target.model,
                    "agent_execution": target.model_dump(),
                }
            ],
        },
    }
    try:
        async with postgres_test_engine() as engine:
            factory = make_session_factory(engine)
            for in_place in [False, True]:
                async with factory() as session:
                    row = await WorkspaceRepository(session).create(
                        repo_url="https://example.com/repo",
                        branch_base="development",
                        task_title="task",
                        task_prompt="fix",
                        test_commands=[],
                        agent="codex",
                        task_policy=policy,
                    )
                    if in_place:
                        row.status = "monitoring_pr"
                        row.pr_url = "https://github.com/example/repo/pull/1"
                        row.pr_number = 1
                        row.branch_name = "awf/source"
                        row.remote_push_branch = "awf/source"
                        row.compose_project_name = "awf_source"
                        row.compose_file_path = "/workspace-owned/compose.yml"
                    await session.commit()
                    source_id = row.id
                async with factory() as session:
                    result = await create_provider_recovery_attempt_row(
                        session,
                        source_id,
                        metadata={
                            "retryable": True,
                            "reason_code": "AGENT_IDLE_TIMEOUT",
                            "failure_fingerprint": "fallback",
                        },
                    )
                    await session.commit()
                async with factory() as session:
                    reloaded = await WorkspaceRepository(session).get(result.new_workspace_id)
                    assert execution_from_policy(reloaded.task_policy) == target
                    assert reloaded.agent == target.agent
                    assert "agent_effort" not in reloaded.task_policy
                    assert result.in_place is in_place
                    assert result.provider_recovery["target_execution"] == target.model_dump()
    finally:
        get_settings.cache_clear()


def test_retired_agent_cannot_promote_third_party_fallback_without_full_contract():
    import pytest

    policy = {
        "provider_recovery": {
            "fallbacks": [{"agent": "codex", "provider": "ollama", "model": "coder"}]
        }
    }
    with pytest.raises(ValueError, match="EXECUTION_FALLBACK_CONTRACT_UNSUPPORTED"):
        _prune_and_migrate_retired_agent(policy, current_agent="gemini")
