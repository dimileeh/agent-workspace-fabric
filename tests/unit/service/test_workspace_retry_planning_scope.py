"""Planning-scope workspace retry tests split from test_workspace_retry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry  # noqa: F401
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS, AgentDefaults
from awf.adapters.registry_api import get_adapter
from awf.common.agent_execution import execution_from_policy, resolve_execution
from awf.common.workspace_policy import CURSOR_AUTO_MODE_POLICY_KEY
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_run_model_for_workspace,
)
from awf.db.enums import AgentRuntime, CursorAutoMode
from awf.db.models import Operation, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService, _PlanningScopeRetryContext
from awf.service.workspaces_retry import _retry_task_policy
from tests.postgres import postgres_test_engine
from tests.unit.service._workspace_retry_helpers import (
    _mark_planning_scope_failed,
    _request,
    _retry_with_preflight_override,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def test_retry_task_policy_planning_scope_fallback_clears_cursor_auto_mode() -> None:
    """Approved fixed fallback must drop Auto mode so executor uses the pin.

    Otherwise ``_agent_run_model_for_workspace`` keeps preferring
    ``auto-smart[...]`` and silently ignores the planning-scope fallback
    (PR #850 review thread PRRT_kwDOSJAM6s6bVKJS).
    """
    source = SimpleNamespace(
        id="ws-planning-scope-auto",
        agent=AgentRuntime.cursor.value,
        failure_reason=None,
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "intelligence"},
    )
    context = _PlanningScopeRetryContext(
        reason_code="AGENT_PLAN_PHASE_SCOPE_VIOLATION",
        evidence={},
        evidence_ref={"event_type": "workspace.state_changed"},
        recovery_strategy="retry_with_fallback_model",
        salvage_policy="discard",
        fallback_model={
            "model": "gpt-5.6-sol",
            "source": "task_policy.planning_scope_recovery.approved_fallback_model",
        },
    )

    policy, target_agent = _retry_task_policy(
        source,
        (),
        planning_scope_context=context,
    )
    workspace = SimpleNamespace(agent=AgentRuntime.cursor.value, task_policy=policy)
    defaults = AgentDefaults(model="auto", effort=None)

    assert target_agent == AgentRuntime.cursor.value
    assert policy["agent_model"] == "gpt-5.6-sol"
    assert CURSOR_AUTO_MODE_POLICY_KEY not in policy
    assert _agent_run_model_for_workspace(workspace) == "gpt-5.6-sol"
    assert _agent_defaults_for_workspace(workspace, defaults) == AgentDefaults(
        model="gpt-5.6-sol",
        effort=None,
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code", "cursor"])
async def test_retry_planning_scope_violation_applies_only_approved_fallback_model(
    factory: async_sessionmaker[AsyncSession],
    agent: str,
) -> None:
    service = WorkspaceService(factory)
    request = _request()
    request.task.agent = AgentRuntime(agent)
    if agent == "cursor":
        request.task.cursor_auto_mode = CursorAutoMode.intelligence
    first = await service.create(request)
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        original = execution_from_policy(source.task_policy)
    assert original is not None
    await _mark_planning_scope_failed(
        factory,
        first.id,
        approved_fallback_model="gpt-5.5",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert retried.task_policy["agent_model"] == "gpt-5.5"
    promoted = execution_from_policy(retried.task_policy)
    assert promoted == original.model_copy(update={"model": "gpt-5.5", "model_explicit": True})
    assert retried.task_policy["agent_provider"] == original.provider
    assert CURSOR_AUTO_MODE_POLICY_KEY not in retried.task_policy
    assert _agent_run_model_for_workspace(retried) == "gpt-5.5"
    adapter = get_adapter(
        agent,
        runner=Mock(),
        defaults=_agent_defaults_for_workspace(
            retried, DEFAULT_AGENT_DEFAULTS[AgentRuntime(agent)]
        ),
    )
    assert adapter.execution == promoted
    assert adapter.default_model == "gpt-5.5"
    assert adapter.get_provider(None) == original.provider
    assert "gpt-5.5" in adapter._cli_args(model=None)
    assert adapter._default_effort == original.effort
    assert operations[0].payload["fallback_model"] == {
        "model": "gpt-5.5",
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
    assert operations[0].result["fallback_model"]["model"] == "gpt-5.5"
    assert retry_created[0].payload["fallback_model"]["model"] == "gpt-5.5"


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("model", ["approved-coder", "embed-only"])
def test_planning_scope_fallback_preserves_frozen_connection(provider, enabled, model, monkeypatch):
    monkeypatch.setattr(
        "awf.common.config.get_settings",
        lambda: SimpleNamespace(third_party_agent_providers_enabled=enabled),
    )
    # Test frozen model-change wiring under a future verified-network gate.
    import awf.service.provider_recovery as recovery

    original = recovery.require_execution_admission

    def verified(selection, *, enabled):
        if not enabled:
            original(selection, enabled=enabled)

    monkeypatch.setattr(recovery, "require_execution_admission", verified)
    selected = resolve_execution(
        agent="codex",
        provider=provider,
        model="original-coder",
        allow_unverified=True,
        profile={
            "services": [{"name": "daemon"}],
            "runtime": {
                "environment": {
                    "AWF_OLLAMA_BASE_URL": "http://daemon:11434",
                    "OPENROUTER_API_KEY": "${SELECTED_ROUTER_KEY}",
                }
            },
        },
    )
    source = SimpleNamespace(
        id="ws-planning-selected",
        agent="codex",
        failure_reason=None,
        task_policy={"agent_execution": selected.model_dump(), "agent_model": selected.model},
    )
    context = _PlanningScopeRetryContext(
        reason_code="AGENT_PLAN_PHASE_SCOPE_VIOLATION",
        evidence={},
        evidence_ref={},
        recovery_strategy="retry_with_fallback_model",
        salvage_policy="discard",
        fallback_model={"model": model},
    )
    if not enabled:
        with pytest.raises(ValueError, match="THIRD_PARTY_AGENT_PROVIDER_ADMISSION_DISABLED"):
            _retry_task_policy(source, (), planning_scope_context=context)
        return
    if model == "embed-only":
        with pytest.raises(ValueError, match="AGENT_PROVIDER_MODEL_INCOMPATIBLE"):
            _retry_task_policy(source, (), planning_scope_context=context)
        return
    policy, agent = _retry_task_policy(source, (), planning_scope_context=context)
    promoted = execution_from_policy(policy)
    assert promoted == selected.model_copy(
        update={"model": "approved-coder", "model_explicit": True}
    )
    workspace = SimpleNamespace(agent=agent, task_policy=policy)
    adapter = get_adapter(
        agent,
        runner=Mock(),
        defaults=_agent_defaults_for_workspace(
            workspace, DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex]
        ),
    )
    assert adapter.get_provider(None) == provider
    assert adapter.default_model == _agent_run_model_for_workspace(workspace) == "approved-coder"
    assert adapter._default_effort is None
    assert source.task_policy["agent_execution"] == selected.model_dump()


async def test_retry_planning_scope_preserves_promoted_fallback_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(first.id)
        assert source is not None
        source.agent = "retired_gemini"
        source.task_policy = {
            **source.task_policy,
            "provider_recovery": {
                "fallbacks": [
                    {"agent": "codex", "model": "gpt-5.5"},
                ],
            },
        }
        await session.commit()

    await _mark_planning_scope_failed(
        factory,
        first.id,
        approved_fallback_model="gemini-2.5-flash",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)

    assert retried is not None
    assert retried.agent == "codex"
    assert retried.task_policy["agent_model"] == "gpt-5.5"


async def test_retry_planning_scope_clears_existing_feature_pr_identity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
    await _mark_planning_scope_failed(factory, first.id)
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_url = "https://github.com/example/retryable/pull/10"
        source.pr_number = 10
        await session.commit()

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)

    assert retried is not None
    assert retried.remote_push_branch is None
    assert retried.pr_url is None
    assert retried.pr_number is None


@pytest.mark.parametrize("provider_constraint", [None, "ollama", "openai"])
@pytest.mark.parametrize("target_model", ["openai/gpt-oss", "ollama/new-coder"])
def test_opencode_planning_model_promotion_recomputes_provider(provider_constraint, target_model):
    selected = resolve_execution(agent="opencode", model="ollama/original-coder")
    policy = {"agent_execution": selected.model_dump(), "agent_model": selected.model}
    if provider_constraint is not None:
        # For a native OpenCode provider constraint, use a consistent source model.
        selected = resolve_execution(
            agent="opencode", model=provider_constraint + "/original-coder"
        )
        policy.update(
            agent_execution=selected.model_dump(),
            agent_model=selected.model,
            agent_provider=provider_constraint,
        )
    source = SimpleNamespace(
        id="ws-opencode-promotion", agent="opencode", failure_reason=None, task_policy=policy
    )
    context = _PlanningScopeRetryContext(
        reason_code="AGENT_PLAN_PHASE_SCOPE_VIOLATION",
        evidence={},
        evidence_ref={},
        recovery_strategy="retry_with_fallback_model",
        salvage_policy="discard",
        fallback_model={"model": target_model},
    )
    provider = target_model.partition("/")[0]
    if provider_constraint is not None and provider_constraint != provider:
        with pytest.raises(ValueError, match="AGENT_PROVIDER_UNSUPPORTED"):
            _retry_task_policy(source, (), planning_scope_context=context)
    else:
        promoted, agent = _retry_task_policy(source, (), planning_scope_context=context)
        execution = execution_from_policy(promoted)
        assert execution == selected.model_copy(
            update={"provider": provider, "model": target_model, "model_explicit": True}
        )
        adapter = get_adapter(
            agent,
            runner=Mock(),
            defaults=_agent_defaults_for_workspace(
                SimpleNamespace(agent=agent, task_policy=promoted),
                DEFAULT_AGENT_DEFAULTS[AgentRuntime.opencode],
            ),
        )
        assert adapter.get_provider(None) == provider
        assert adapter.default_model == target_model
    assert source.task_policy == policy
