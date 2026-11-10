"""Operator control: raise a managed OpenRouter budget, patch the live key, resume."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.db.enums import FailureReason, OperationType, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import ProviderManagedKeyRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.controls import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlService,
)
from awf.service.controls_provider_budget import ProviderBudgetControlError
from awf.service.provider_managed_keys import ManagedKeyBroker
from tests.postgres import postgres_test_engine
from tests.shared.openrouter_management import FakeOpenRouterManagement

pytestmark = pytest.mark.unit

_SELECTION = resolve_execution(
    agent="codex",
    provider="openrouter",
    model="moonshotai/kimi-k2.7-code",
    allow_unverified=True,
    managed_openrouter=True,
)


async def _workspace(
    factory,
    fake: FakeOpenRouterManagement,
    tmp_path,
    *,
    status: WorkspaceStatus = WorkspaceStatus.running,
    park: bool = True,
    managed: bool = True,
    finalized_spend: float = 0.0,
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.task_policy = (
            {
                EXECUTION_POLICY_KEY: _SELECTION.model_dump(mode="json"),
                "provider_budget": {
                    "version": 1,
                    "provider": "openrouter",
                    "managed": True,
                    "limit_usd": 1.0,
                    "requested_usd": None,
                    "source": "default",
                },
            }
            if managed
            else {}
        )
        steps = ["provisioning", "ready", "running"]
        if status == WorkspaceStatus.monitoring_pr:
            steps.append("monitoring_pr")
        for step in steps:
            await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
        await session.commit()
        workspace_id = ws.id
    if not managed:
        return workspace_id
    broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
    if finalized_spend:
        earlier = await broker.acquire(workspace_id=workspace_id, selection=_SELECTION, purpose="a")
        fake.set_usage(earlier.key_hash, finalized_spend)
        await earlier.release(failed=False)
    live = await broker.acquire(workspace_id=workspace_id, selection=_SELECTION, purpose="agent")
    fake.set_usage(live.key_hash, 0.5)
    await live.heartbeat()
    if park:
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=SimpleNamespace(),
            compose=SimpleNamespace(),
            validation=SimpleNamespace(),
            pr_creator=SimpleNamespace(),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "w", compose_projects_root=tmp_path / "c"
            ),
        )
        await executor._mark_failed(
            workspace_id=workspace_id,
            from_status=status,
            failure_reason=FailureReason.agent_failure,
            message="exhausted",
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
        )
    return workspace_id


async def _set(factory, workspace_id: str, fake, **kwargs):
    kwargs.setdefault("usd", 5)
    kwargs.setdefault("idempotency_key", "raise-1")
    async with factory() as session:
        service = WorkspaceControlService(
            session, project_stopper=SimpleNamespace(), cleaner_factory=SimpleNamespace()
        )
        response = await service.set_provider_budget(
            workspace_id, management_client=fake.client() if fake else None, **kwargs
        )
        await session.commit()
        return response


async def _load(factory, workspace_id: str):
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        events = (
            (
                await session.execute(
                    select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        keys = await ProviderManagedKeyRepository(session).for_workspace(workspace_id)
        return ws, events, keys


async def test_raise_patches_the_live_key_audits_and_arms_the_pre_pr_resume(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path, finalized_spend=0.25)
        before, _events, _keys = await _load(factory, workspace_id)
        assert before.status == WorkspaceStatus.blocked.value

        response = await _set(factory, workspace_id, fake, usd=5, reason="more budget")

        patch = [call for call in fake.calls if call[0] == "PATCH"]
        assert patch == [("PATCH", "/keys/hash2", {"limit": 4.75})]
        ws, events, keys = await _load(factory, workspace_id)
        assert response.operation_status == "succeeded"
        assert ws.version > before.version
        assert ws.task_policy["provider_budget"]["limit_usd"] == 5.0
        assert ws.task_policy["provider_budget"]["source"] == "operator"
        assert ws.task_policy["provider_budget"]["requested_usd"] is None
        assert keys[-1].limit_usd == Decimal("4.75")
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.pending_operator_hint["reason"] == "provider_budget_raised"
        assert "5.000000 USD" in ws.pending_operator_hint["directive"]
        raised = next(e for e in events if e.event_type == "provider.budget.raised")
        assert raised.reason_code == "PROVIDER_BUDGET_RAISED"
        assert raised.payload["old_limit_usd"] == "1.000000"
        assert raised.payload["new_limit_usd"] == "5.000000"
        assert raised.payload["spend_usd"] == "0.750000"
        assert raised.payload["resumed"] is True
        assert any(
            e.event_type == "workspace.audit.control_operation"
            and e.payload.get("action") == OperationType.provider_budget.value
            for e in events
        )
        async with factory() as session:
            resumable = await WorkspaceRepository(session).list_resumable_blocked_ids(limit=10)
        assert workspace_id in resumable


async def test_raise_returns_a_monitor_origin_park_to_the_pr_monitor(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(
            factory, fake, tmp_path, status=WorkspaceStatus.monitoring_pr
        )

        await _set(factory, workspace_id, fake)

        ws, _events, _keys = await _load(factory, workspace_id)
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_claimed_by is None


async def test_raise_on_a_running_workspace_only_patches_the_key(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path, park=False)

        await _set(factory, workspace_id, fake, usd=2)

        ws, events, _keys = await _load(factory, workspace_id)
        assert ws.status == WorkspaceStatus.running.value
        assert ws.pending_operator_hint is None
        raised = next(e for e in events if e.event_type == "provider.budget.raised")
        assert raised.payload["resumed"] is False
        assert ("PATCH", "/keys/hash1", {"limit": 2.0}) in fake.calls


async def test_replays_conflicts_and_version_fences(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path)
        first = await _set(factory, workspace_id, fake)

        replay = await _set(factory, workspace_id, fake)
        assert replay.operation_id == first.operation_id
        with pytest.raises(IdempotencyConflictError):
            await _set(factory, workspace_id, fake, usd=6)
        with pytest.raises(VersionConflictError):
            await _set(factory, workspace_id, fake, idempotency_key="k2", expected_version=1)


@pytest.mark.parametrize("usd", [0.75, 0.1, 0])
async def test_budgets_at_or_below_recorded_spend_are_invalid(tmp_path, usd):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path, finalized_spend=0.25)

        with pytest.raises(ProviderBudgetControlError) as raised:
            await _set(factory, workspace_id, fake, usd=usd)

        assert raised.value.error_code == "PROVIDER_BUDGET_INVALID"
        assert raised.value.http_status == 422
        assert not [call for call in fake.calls if call[0] == "PATCH"]


async def test_unmanaged_and_terminal_workspaces_are_rejected(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        static = await _workspace(factory, fake, tmp_path, managed=False)
        with pytest.raises(ProviderBudgetControlError) as raised:
            await _set(factory, static, fake)
        assert (raised.value.error_code, raised.value.http_status) == (
            "PROVIDER_BUDGET_UNSUPPORTED",
            409,
        )
        terminal = await _workspace(factory, fake, tmp_path, park=False)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(terminal)
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="test")
            await session.commit()
        with pytest.raises(ProviderBudgetControlError) as raised:
            await _set(factory, terminal, fake, idempotency_key="terminal")
        assert raised.value.error_code == "PROVIDER_BUDGET_UNSUPPORTED"
        assert raised.value.detail == {"status": "failed"}


@pytest.mark.parametrize(
    ("client", "code"),
    [("fail", "PROVIDER_MANAGEMENT_API_UNAVAILABLE"), (None, "PROVIDER_MANAGEMENT_KEY_MISSING")],
)
async def test_live_key_patch_failures_leave_no_state_change(tmp_path, client, code):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path)
        before, before_events, _keys = await _load(factory, workspace_id)
        fake.fail[("PATCH", "key")] = 503

        with pytest.raises(ProviderBudgetControlError) as raised:
            await _set(factory, workspace_id, fake if client else None)

        assert (raised.value.error_code, raised.value.http_status) == (code, 502)
        ws, events, _keys = await _load(factory, workspace_id)
        assert ws.version == before.version
        assert ws.task_policy["provider_budget"]["limit_usd"] == 1.0
        assert len(events) == len(before_events)


async def test_without_a_live_key_the_raise_needs_no_management_api(tmp_path):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, fake, tmp_path)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        await broker.finalize_workspace_keys(workspace_id)

        await _set(factory, workspace_id, None, usd=3)

        ws, _events, _keys = await _load(factory, workspace_id)
        assert ws.task_policy["provider_budget"]["limit_usd"] == 3.0
