"""A monitor repair that exhausts its managed key parks the PR owner for a human."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.run_results import AgentRunError
from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.common.commands import CommandResult
from awf.control.provider_budget_park import (
    MONITOR_PROVIDER_BUDGET_RESUME_PHASE,
    PROVIDER_BUDGET_BLOCK_TYPE,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import lifecycle, provider_ops
from awf.runtime.pr_monitor_runner.loop_recovery_ops import (
    _provider_recovery_operation_result_updates,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderManagedKeyParkedError,
    ProviderRecoveryAuthError,
)
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit

_BUDGET = {"budget_usd": "1.000000", "spend_usd": "1.000000", "remaining_usd": "0.000000"}


async def _monitoring(factory) -> str:
    selected = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="m/x",
        allow_unverified=True,
        managed_openrouter=True,
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed monitor",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.task_policy = {
            EXECUTION_POLICY_KEY: selected.model_dump(mode="json"),
            "provider_budget": {"managed": True, "limit_usd": 1.0},
        }
        for step in ("provisioning", "ready", "running", "monitoring_pr"):
            await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
        await session.commit()
        return ws.id


def _runner(factory, owner: str | None = None):
    return SimpleNamespace(_deps=SimpleNamespace(session_factory=factory), _monitor_owner_id=owner)


async def test_terminal_sink_parks_a_monitor_on_its_managed_key_budget():
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _monitoring(factory)

        failed = await lifecycle._terminate_failed(
            _runner(factory),
            workspace_id,
            message="monitor: managed provider key unavailable (PROVIDER_BUDGET_EXHAUSTED)",
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
            details={"provider_budget": _BUDGET},
            failure_reason=FailureReason.agent_failure,
        )

        assert failed is False
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_type == PROVIDER_BUDGET_BLOCK_TYPE
        assert ws.block_reason_code == "PROVIDER_BUDGET_EXHAUSTED"
        assert ws.block_resume_phase == MONITOR_PROVIDER_BUDGET_RESUME_PHASE
        assert ws.failure_reason is None


async def test_terminal_sink_still_fails_other_reasons():
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _monitoring(factory)

        failed = await lifecycle._terminate_failed(
            _runner(factory), workspace_id, message="boom", reason_code="MONITOR_ABORT"
        )

        assert failed is True
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == WorkspaceStatus.failed.value


async def test_a_superseded_park_falls_back_to_the_normal_ignored_callback():
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _monitoring(factory)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            ws.monitor_claimed_by = "other-worker"
            await session.commit()

        failed = await lifecycle._terminate_failed(
            _runner(factory, owner="stale-worker"),
            workspace_id,
            message="exhausted",
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
        )

        assert failed is False
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.parametrize(
    "reason",
    [
        "PROVIDER_BUDGET_EXHAUSTED",
        "PROVIDER_MANAGED_KEY_UNAVAILABLE",
        "PROVIDER_MANAGEMENT_KEY_MISSING",
    ],
)
async def test_repair_failures_unwind_as_a_park_without_recovery(reason):
    runner = SimpleNamespace(
        _persist_state=AsyncMock(), _record_provider_agent_run_error=AsyncMock()
    )
    error = AgentRunError(
        agent="codex",
        result=CommandResult(returncode=1, stdout="", stderr="402 Payment Required"),
        reason_code=reason,
        details={"retryable": False, "provider_budget": _BUDGET},
    )

    with pytest.raises(ProviderManagedKeyParkedError) as raised:
        await provider_ops._handle_provider_agent_run_error(
            runner, "ws_x", error, state=SimpleNamespace()
        )

    assert isinstance(raised.value, ProviderRecoveryAuthError)
    assert raised.value.reason_code == reason
    runner._persist_state.assert_awaited_once()
    runner._record_provider_agent_run_error.assert_not_awaited()
    assert _provider_recovery_operation_result_updates(raised.value) == {
        "outcome": "provider_key_parked",
        "reason_code": reason,
    }
    assert _provider_recovery_operation_result_updates(ProviderRecoveryAuthError()) == {}
