"""A fallback promotes its complete connection, never source credentials/effort."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from awf.common.agent_execution import execution_from_policy, resolve_execution
from awf.service.provider_recovery import decide_provider_recovery, install_recovery_selection
from awf.service.workspaces_retry_recovery import _prune_and_migrate_retired_agent


@pytest.fixture
def verified_future_network(monkeypatch):
    """Exercise dormant full-selection wiring after a future network gate passes."""
    import awf.common.agent_execution as execution
    import awf.service.provider_recovery as recovery

    original = execution.require_execution_admission

    def verified(selection, *, enabled):
        if not enabled:
            original(selection, enabled=enabled)

    monkeypatch.setattr(recovery, "require_execution_admission", verified)
    monkeypatch.setattr(execution, "require_execution_admission", verified)


def test_full_fallback_selection_promotion(monkeypatch, verified_future_network):
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


async def test_persisted_monitor_and_retry_fallback_promote_entire_snapshot(
    monkeypatch, verified_future_network
):
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


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
@pytest.mark.parametrize("enabled", [False, True])
def test_foundation_blocks_all_third_party_promotions(monkeypatch, provider, enabled):
    from types import SimpleNamespace

    from awf.service.provider_recovery import (
        ProviderRecoveryDecision,
        _install_fixed_recovery_model,
    )

    monkeypatch.setattr(
        "awf.common.config.get_settings",
        lambda: SimpleNamespace(third_party_agent_providers_enabled=enabled),
    )
    target = resolve_execution(
        agent="codex", provider=provider, model="coder", allow_unverified=True
    )
    policy = {
        "agent_execution": resolve_execution(agent="codex").model_dump(),
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent": target.agent,
                    "provider": target.provider,
                    "model": target.model,
                    "agent_execution": target.model_dump(),
                }
            ]
        },
    }
    decision = ProviderRecoveryDecision(
        action="fallback",
        reason_code="fallback",
        target_agent=target.agent,
        retryable=True,
        not_before=None,
        terminal_reason=None,
        fallback_attempt_number=1,
        retry_attempt_number=0,
        target_provider=target.provider,
        target_model=target.model,
    )
    expected = "EXECUTION_NETWORK_PROBE_UNSUPPORTED" if enabled else "ADMISSION_DISABLED"
    with pytest.raises(ValueError, match=expected):
        install_recovery_selection(policy, decision)
    with pytest.raises(ValueError, match=expected):
        _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    with pytest.raises(ValueError, match=expected):
        _install_fixed_recovery_model({"agent_execution": target.model_dump()}, "new-coder")
    assert execution_from_policy(policy).provider == "openai"


def test_implicit_native_capacity_fallback_preserves_frozen_contract():
    source = resolve_execution(agent="codex", model="gpt-5.3-codex-spark")
    policy = {
        "agent_execution": source.model_dump(),
        "agent_model": source.model,
        "provider_recovery": {"max_same_provider_retries": 0},
    }
    decision = decide_provider_recovery(
        {"retryable": True, "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED"},
        task_policy=policy,
        current_agent="codex",
        current_model=source.model,
        now=datetime.now(UTC),
    )
    assert decision.action == "fallback"
    promoted = execution_from_policy(install_recovery_selection(policy, decision))
    assert promoted == source.model_copy(
        update={"model": decision.target_model, "model_explicit": True}
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_disabled_third_party_admission_preserves_native_retired_agent_fallback(monkeypatch, agent):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "awf.common.config.get_settings",
        lambda: SimpleNamespace(third_party_agent_providers_enabled=False),
    )
    target = resolve_execution(agent=agent)
    policy = {
        "agent_effort": "xhigh",
        "cursor_auto_mode": "cost",
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent": target.agent,
                    "provider": target.provider,
                    "model": target.model,
                    "agent_execution": target.model_dump(),
                }
            ]
        },
    }
    promoted, runtime = _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    assert runtime == target.agent
    assert execution_from_policy(promoted) == target
    assert promoted["provider_recovery"]["fallbacks"] == [None]
    assert promoted.get("agent_effort") == target.effort
    assert "cursor_auto_mode" not in promoted


@pytest.mark.parametrize(
    "source_provider,target_provider", [("openai", "ollama"), ("ollama", "openai")]
)
def test_legacy_fallback_cannot_cross_third_party_contract_boundary(
    source_provider, target_provider
):
    source = resolve_execution(
        agent="codex", provider=source_provider, model="source-model", allow_unverified=True
    )
    fallback = {"agent": "codex", "provider": target_provider, "model": "target-model"}
    policy = {
        "agent_execution": source.model_dump(),
        "agent_model": source.model,
        "provider_recovery": {"max_same_provider_retries": 0, "fallbacks": [fallback]},
    }
    original = deepcopy(policy)
    decision = decide_provider_recovery(
        {"retryable": True, "reason_code": "AGENT_PROVIDER_RATE_LIMITED"},
        task_policy=policy,
        current_agent="codex",
        current_model=source.model,
        now=datetime.now(UTC),
    )
    assert decision.action == "fallback" and decision.target_provider == target_provider
    with pytest.raises(ValueError, match="^EXECUTION_FALLBACK_CONTRACT_UNSUPPORTED$"):
        install_recovery_selection(policy, decision)
    assert policy == original


def test_promotion_skips_consumed_fallback_without_rebinding_its_connection():
    target = resolve_execution(agent="claude_code", model="claude-test")
    policy = {
        "agent_execution": resolve_execution(agent="codex").model_dump(),
        "provider_recovery": {
            "max_same_provider_retries": 0,
            "fallbacks": [
                None,
                {
                    "agent": target.agent,
                    "provider": target.provider,
                    "model": target.model,
                    "agent_execution": target.model_dump(),
                },
            ],
        },
    }
    original = deepcopy(policy)
    decision = decide_provider_recovery(
        {"retryable": True},
        task_policy=policy,
        current_agent="codex",
        current_model="native",
        now=datetime.now(UTC),
    )
    assert decision.action == "fallback" and decision.fallback_attempt_number == 2
    assert execution_from_policy(install_recovery_selection(policy, decision)) == target
    assert policy == original


@pytest.mark.parametrize(
    "recovery",
    [
        {"fallbacks": []},
        {"fallbacks": [None]},
        {"fallbacks": [{"agent": "codex", "model": "gpt-test"}], "max_fallback_attempts": 0},
    ],
)
def test_unavailable_fallbacks_do_not_advertise_launchability(recovery):
    from awf.service.provider_recovery import has_approved_launchable_fallback

    assert has_approved_launchable_fallback({"provider_recovery": recovery}) is False


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
async def test_recovery_records_frozen_provider_and_scoped_circuit_despite_stale_failure_metadata(
    provider,
):
    from awf.db.repositories import ProviderModelCircuitBreakerRepository, WorkspaceRepository
    from awf.service.provider_recovery import create_provider_recovery_attempt_row
    from tests.postgres import postgres_test_session

    selected = resolve_execution(
        agent="codex",
        provider=provider,
        model="selected-coder",
        allow_unverified=True,
        profile={
            "runtime": {"environment": {"OLLAMA_HOST": "http://daemon:11434"}},
            "services": {"daemon": {}},
        },
    )
    async with postgres_test_session() as session:
        repo = WorkspaceRepository(session)
        row = await repo.create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="task",
            task_prompt="fix",
            test_commands=[],
            agent="codex",
            task_policy={"agent_execution": selected.model_dump(), "agent_model": selected.model},
        )
        await session.flush()
        result = await create_provider_recovery_attempt_row(
            session,
            row.id,
            metadata={
                "retryable": True,
                "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                "failure_fingerprint": "scoped-failure",
                "provider": "openai",
                "model": "stale-native-model",
            },
        )
        assert result.action == "retry"
        assert result.provider_recovery["provider"] == selected.provider
        assert result.provider_recovery["model"] == selected.model
        retried = await repo.get(result.new_workspace_id)
        assert execution_from_policy(retried.task_policy) == selected
        scope = selected.connection_scope(row.id)
        assert result.provider_recovery["connection_scope"] == scope
        circuits = ProviderModelCircuitBreakerRepository(session)
        recorded = await circuits.get(
            provider=provider, model=selected.model, connection_scope=scope
        )
        assert recorded is not None and recorded.failure_count == 1
        assert recorded.last_workspace_id == row.id
        assert await circuits.get(provider="openai", model="stale-native-model") is None
