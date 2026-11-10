"""Managed-key budget failures park for a human; they never fail, retry or fall back."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.control.provider_budget_park import (
    MONITOR_PROVIDER_BUDGET_RESUME_PHASE,
    PROVIDER_BUDGET_BLOCK_TYPE,
    PROVIDER_BUDGET_RESUME_PHASE,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ProviderModelCircuitBreaker, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_recovery import provider_recovery_metadata_from_failure
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit

_SELECTION = resolve_execution(
    agent="codex",
    provider="openrouter",
    model="moonshotai/kimi-k2.7-code",
    allow_unverified=True,
    managed_openrouter=True,
)
_POLICY = {
    EXECUTION_POLICY_KEY: _SELECTION.model_dump(mode="json"),
    "provider_budget": {"version": 1, "managed": True, "limit_usd": 1.0, "source": "default"},
    "provider_recovery": {
        "max_attempts": 3,
        "fallbacks": [{"agent": "claude_code", "provider": "anthropic", "model": "native"}],
    },
}
_BUDGET = {"budget_usd": "1.000000", "spend_usd": "1.000000", "remaining_usd": "0.000000"}


async def _workspace(factory, status: WorkspaceStatus) -> str:
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
        ws.task_policy = dict(_POLICY)
        path = {
            WorkspaceStatus.running: ("provisioning", "ready", "running"),
            WorkspaceStatus.validating: ("provisioning", "ready", "running", "validating"),
            WorkspaceStatus.monitoring_pr: ("provisioning", "ready", "running", "monitoring_pr"),
            WorkspaceStatus.provisioning: ("provisioning",),
        }[status]
        for step in path:
            await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
        await session.commit()
        return ws.id


def _executor(factory, tmp_path) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=factory,
        runner=SimpleNamespace(),
        compose=SimpleNamespace(),
        validation=SimpleNamespace(),
        pr_creator=SimpleNamespace(),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees", compose_projects_root=tmp_path / "compose"
        ),
    )


async def _events(factory, workspace_id: str) -> list[WorkspaceEvent]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(WorkspaceEvent.workspace_id == workspace_id)
                    .order_by(WorkspaceEvent.event_order)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.parametrize(
    ("reason", "event"),
    [
        ("PROVIDER_BUDGET_EXHAUSTED", "provider.budget.exhausted"),
        ("PROVIDER_ACCOUNT_CREDITS_EXHAUSTED", "provider.account.credits_exhausted"),
        ("PROVIDER_MANAGED_KEY_UNAVAILABLE", "provider.managed_key.unavailable"),
        ("PROVIDER_MANAGEMENT_KEY_MISSING", "provider.managed_key.unavailable"),
    ],
)
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (WorkspaceStatus.running, PROVIDER_BUDGET_RESUME_PHASE),
        (WorkspaceStatus.validating, PROVIDER_BUDGET_RESUME_PHASE),
        (WorkspaceStatus.monitoring_pr, MONITOR_PROVIDER_BUDGET_RESUME_PHASE),
    ],
)
async def test_executor_failure_sink_parks_managed_key_failures(
    tmp_path, reason, event, status, phase
):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, status)

        await _executor(factory, tmp_path)._mark_failed(
            workspace_id=workspace_id,
            from_status=status,
            failure_reason=FailureReason.agent_failure,
            message="codex exited 1 (PROVIDER_BUDGET_EXHAUSTED): insufficient credits",
            reason_code=reason,
            details={"retryable": False, "provider_budget": _BUDGET},
        )

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            breakers = await session.scalar(
                select(func.count()).select_from(ProviderModelCircuitBreaker)
            )
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_type == PROVIDER_BUDGET_BLOCK_TYPE
        assert ws.block_reason_code == reason
        assert ws.block_resume_phase == phase
        assert ws.failure_reason is None
        assert breakers == 0
        events = await _events(factory, workspace_id)
        types = [e.event_type for e in events]
        assert "workspace.attention_required" in types and event in types
        parked = next(e for e in events if e.event_type == event)
        assert parked.reason_code == reason
        assert parked.payload["provider_budget"] == _BUDGET


@pytest.mark.parametrize(
    "status", [WorkspaceStatus.running, WorkspaceStatus.monitoring_pr], ids=["executor", "monitor"]
)
async def test_account_credit_park_names_the_account_balance_not_the_budget(tmp_path, status):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, status)

        await _executor(factory, tmp_path)._mark_failed(
            workspace_id=workspace_id,
            from_status=status,
            failure_reason=FailureReason.agent_failure,
            message="codex exited 1 (PROVIDER_ACCOUNT_CREDITS_EXHAUSTED): 402 Payment Required",
            reason_code="PROVIDER_ACCOUNT_CREDITS_EXHAUSTED",
            details={"retryable": False, "provider_budget": _BUDGET},
        )

        events = await _events(factory, workspace_id)
        parked = next(e for e in events if e.event_type == "provider.account.credits_exhausted")
        message = parked.payload["message"]
        assert "OpenRouter account" in message
        assert "not the workspace budget" in message
        assert message.endswith("402 Payment Required")


async def test_other_failures_and_unparkable_statuses_still_fail_normally(tmp_path):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        running = await _workspace(factory, WorkspaceStatus.running)
        provisioning = await _workspace(factory, WorkspaceStatus.provisioning)
        executor = _executor(factory, tmp_path)

        await executor._mark_failed(
            workspace_id=running,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message="boom",
            reason_code="AGENT_CLI_FAILED",
        )
        await executor._mark_failed(
            workspace_id=provisioning,
            from_status=WorkspaceStatus.provisioning,
            failure_reason=FailureReason.infrastructure_failure,
            message="no key",
            reason_code="PROVIDER_MANAGEMENT_KEY_MISSING",
        )

        async with factory() as session:
            repo = WorkspaceRepository(session)
            assert (await repo.get(running)).status == WorkspaceStatus.failed.value
            assert (await repo.get(provisioning)).status == WorkspaceStatus.failed.value


async def test_a_raced_row_is_not_parked_twice(tmp_path):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, WorkspaceStatus.running)
        executor = _executor(factory, tmp_path)
        kwargs = {
            "workspace_id": workspace_id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.agent_failure,
            "message": "exhausted",
            "reason_code": "PROVIDER_BUDGET_EXHAUSTED",
        }
        await executor._mark_failed(**kwargs)
        await executor._mark_failed(**kwargs)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == WorkspaceStatus.blocked.value
        types = [e.event_type for e in await _events(factory, workspace_id)]
        assert types.count("provider.budget.exhausted") == 1


@pytest.mark.parametrize(
    "reason",
    [
        "PROVIDER_BUDGET_EXHAUSTED",
        "PROVIDER_MANAGED_KEY_UNAVAILABLE",
        "PROVIDER_MANAGEMENT_KEY_MISSING",
    ],
)
def test_park_codes_never_produce_provider_recovery_metadata(reason):
    assert (
        provider_recovery_metadata_from_failure(
            reason_code=reason,
            message="quota exceeded: insufficient credits, rate limit",
            details={"provider_recovery": {"retryable": True, "failure_type": "quota"}},
            task_policy=_POLICY,
        )
        is None
    )


async def test_recovering_divert_treats_park_codes_as_terminal(tmp_path):
    from awf.control.executor.state_ops import ProviderFailureDivert

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, WorkspaceStatus.running)

        divert = await _executor(factory, tmp_path).enter_recovering_for_provider_failure(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            message="insufficient credits; quota exceeded",
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
            details={"provider_recovery": {"retryable": True, "failure_type": "quota"}},
        )

        assert divert is ProviderFailureDivert.terminal
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == WorkspaceStatus.running.value


async def test_blocked_resume_reinvokes_the_agent_with_the_budget_directive(tmp_path):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _workspace(factory, WorkspaceStatus.running)
        executor = _executor(factory, tmp_path)
        await executor._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message="exhausted",
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            ws.pending_operator_hint = {
                "directive": "Budget raised to 5 USD; continue the task.",
                "reason": "provider_budget_raised",
            }
            ids = await repo.list_resumable_blocked_ids(limit=10)
            assert workspace_id in ids
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="test-resume")
            ws.execution_claimed_by = "worker-a"
            await session.commit()

        begun = await executor._begin_execution(
            workspace_id,
            resume_reason="blocked",
            execution_owner_id="worker-a",
            execution_lease_expires_at=None,
        )

        assert begun is not None
        resumed, skip_agent, _disable_fix_passes, _baseline = begun
        assert skip_agent is False
        assert (
            "[Operator directive — continue after the operator raised the provider budget]"
            in resumed.task_prompt
        )
        assert "Budget raised to 5 USD" in resumed.task_prompt
