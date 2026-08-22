"""Planning-scope workspace retry tests split from test_workspace_retry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.defaults import AgentDefaults
from awf.common.workspace_policy import CURSOR_AUTO_MODE_POLICY_KEY
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_run_model_for_workspace,
)
from awf.db.enums import AgentRuntime
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


async def test_retry_planning_scope_violation_applies_only_approved_fallback_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
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
    assert operations[0].payload["fallback_model"] == {
        "model": "gpt-5.5",
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
    assert operations[0].result["fallback_model"]["model"] == "gpt-5.5"
    assert retry_created[0].payload["fallback_model"]["model"] == "gpt-5.5"


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
