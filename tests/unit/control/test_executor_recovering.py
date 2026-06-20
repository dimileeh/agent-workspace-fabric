"""Executor-side in-place provider-recovery (``recovering``) tests (#612).

Covers the ENTER divert (``enter_recovering_for_provider_failure`` — the
hoisted classify+budget decision that pauses a retryable provider failure into
``recovering`` BEFORE the terminal teardown, T3/T7), the ``_begin_execution``
recovering branch that bypasses every blocked-only resume semantic (T9), and the
post-agent-commit fork wiring (the same divert at the second agent-failure fork).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import quality_methods as executor_quality_methods
from awf.control.executor.quality_gates import _PostAgentCommitStepError
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_recovery import provider_cooldown_not_before
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _make_executor,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]  # noqa: SLF001
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


async def _seed_running(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    task_policy: dict | None = None,
    execution_claimed_by: str | None = "worker-A",
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="recovering",
            task_prompt="do the thing",
            agent=agent,
            test_commands=["pytest -q"],
            task_policy=task_policy,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        ws.execution_claimed_by = execution_claimed_by
        await s.commit()
        return ws.id


_IDLE_DETAILS = {"provider": "openai", "model": "gpt-5"}


@pytest.mark.unit
async def test_enter_recovering_diverts_retryable_failure_with_budget(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_running(factory, task_policy={"agent_model": "gpt-5"})
    executor = _make_executor(fake, factory, tmp_path)

    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="idle timeout exceeded after 600s",
        reason_code="AGENT_IDLE_TIMEOUT",
        details=_IDLE_DETAILS,
        execution_owner_id="worker-A",
    )

    assert diverted is True
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value
        # The warm-stack lease (execution claim) is preserved.
        assert ws.execution_claimed_by == "worker-A"
        # The cooldown deadline is persisted for the worker resume gate.
        assert provider_cooldown_not_before(ws.task_policy) is not None
        assert ws.task_policy["provider_recovery_state"]["retry_attempt_number"] == 1


@pytest.mark.unit
async def test_enter_recovering_budget_exhausted_stays_failed_path(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Budget already spent → no in-place divert; the caller falls through to the
    # terminal ``_mark_failed`` path, so the row stays ``running`` here.
    ws_id = await _seed_running(
        factory,
        task_policy={
            "agent_model": "gpt-5",
            "provider_recovery_state": {"retry_attempt_number": 1},
        },
    )
    executor = _make_executor(fake, factory, tmp_path)

    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="idle timeout exceeded after 600s",
        reason_code="AGENT_IDLE_TIMEOUT",
        details=_IDLE_DETAILS,
        execution_owner_id="worker-A",
    )

    assert diverted is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_enter_recovering_non_retryable_stays_failed_path(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_running(factory)
    executor = _make_executor(fake, factory, tmp_path)

    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="the agent gave up",
        reason_code="AGENT_FAILED",
        details={},
        execution_owner_id="worker-A",
    )

    assert diverted is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_enter_recovering_owner_mismatch_does_not_clobber(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A stale executor whose claim was transferred to a newer claimant must not
    # win the CAS — the epoch fence (execution_claimed_by) matches 0 rows.
    ws_id = await _seed_running(factory, execution_claimed_by="worker-new")
    executor = _make_executor(fake, factory, tmp_path)

    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="idle timeout exceeded after 600s",
        reason_code="AGENT_IDLE_TIMEOUT",
        details=_IDLE_DETAILS,
        execution_owner_id="worker-stale",
    )

    assert diverted is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value
        assert ws.execution_claimed_by == "worker-new"


@pytest.mark.unit
async def test_enter_recovering_callback_terminal_race_does_not_clobber(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The row raced into a callback-terminal status (e.g. an operator cancel)
    # after the failure but before the enter CAS: the divert must fail closed
    # (0 rows) and finalize any ignored stale callback rather than clobbering it.
    ws_id = await _seed_running(factory, task_policy={"agent_model": "gpt-5"})
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCEL")
        await s.commit()

    executor = _make_executor(fake, factory, tmp_path)
    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="idle timeout exceeded after 600s",
        reason_code="AGENT_IDLE_TIMEOUT",
        details=_IDLE_DETAILS,
        execution_owner_id="worker-A",
    )

    assert diverted is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.cancelled.value


@pytest.mark.unit
async def test_enter_recovering_invalid_agent_resolves_no_default_model(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # An unknown agent runtime cannot resolve a configured default model (the
    # ``AgentRuntime(...)`` lookup raises), so the in-place decision proceeds with
    # no effective default — the same defensive guard as ``_prepare_provider_recovery``.
    ws_id = await _seed_running(factory, agent="bogus-runtime")
    executor = _make_executor(fake, factory, tmp_path)

    diverted = await executor.enter_recovering_for_provider_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        message="idle timeout exceeded after 600s",
        reason_code="AGENT_IDLE_TIMEOUT",
        details=_IDLE_DETAILS,
        execution_owner_id="worker-A",
    )

    assert diverted is True
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value


@pytest.mark.unit
async def test_resume_recovering_execution_short_circuits_when_not_running(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The worker's resume CAS never landed (the row is still ``recovering``), so
    # ``resume_recovering_execution`` fails its ``running`` recheck in
    # ``_begin_execution`` and returns without invoking the agent.
    ws_id = await _seed_running(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.recovering, reason_code="SEED")
        await s.commit()

    executor = _make_executor(fake, factory, tmp_path)
    await executor.resume_recovering_execution(ws_id, execution_owner_id="worker-A")

    assert fake.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value


@pytest.mark.unit
async def test_begin_execution_recovering_bypasses_blocked_semantics(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A ``recovering`` resume is a fresh agent re-run: it must NOT reuse a
    # persisted baseline, inject a stray operator hint, or honor grants — even if
    # the row happens to carry them.
    ws_id = await _seed_running(factory, execution_claimed_by="worker-A")
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.block_baseline_coverage = {"provider": "python", "percent": 80.0}
        ws.pending_operator_hint = {
            "status": "pending",
            "directive": "revert it",
            "reason": "r",
        }
        ws.block_epoch = 1
        s.add(
            OperatorGrantAuditRecord(
                id=f"grant_{ws.id}",
                workspace_id=ws.id,
                operator="op@example.com",
                reason="approved",
                normalized_path="pyproject.toml",
                block_epoch=1,
            )
        )
        original_prompt = ws.task_prompt
        await s.commit()

    executor = _make_executor(fake, factory, tmp_path)
    begin = await executor._begin_execution(  # noqa: SLF001
        ws_id,
        resume_reason="recovering",
        execution_owner_id="worker-A",
        execution_lease_expires_at=None,
    )

    assert begin is not None
    ws, resume_skip_agent, resume_disable_fix_passes, baseline_coverage = begin
    assert resume_skip_agent is False
    assert resume_disable_fix_passes is False
    assert baseline_coverage is None
    # The stray operator directive was NOT appended to the prompt.
    assert ws.task_prompt == original_prompt


@pytest.mark.unit
async def test_post_agent_commit_fork_diverts_to_recovering() -> None:
    # When the in-place divert fires, the post-agent-commit fork must NOT mark
    # the workspace failed or queue the fresh-relaunch provider recovery.
    executor = SimpleNamespace(
        enter_recovering_for_provider_failure=AsyncMock(return_value=True),
        _mark_failed=AsyncMock(),
        _prepare_provider_recovery=AsyncMock(),
    )
    error = _PostAgentCommitStepError(
        stage="git commit",
        result=CommandResult(returncode=1, stdout="", stderr="boom"),
        classification=None,
    )

    await executor_quality_methods._mark_post_agent_commit_failed(  # noqa: SLF001
        executor,
        workspace_id="ws_div",
        error=error,
        agent_run_reason_code="AGENT_IDLE_TIMEOUT",
        agent_run_details={"provider": "openai", "model": "gpt-5"},
        agent_exit_note=None,
        upstream_failure_reason=FailureReason.agent_failure,
        execution_owner_id="worker-A",
    )

    executor.enter_recovering_for_provider_failure.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_post_agent_commit_fork_falls_through_when_not_diverted() -> None:
    # Regression: when the divert declines (budget exhausted / non-retryable),
    # the fork keeps today's terminal _mark_failed + _prepare_provider_recovery.
    executor = SimpleNamespace(
        enter_recovering_for_provider_failure=AsyncMock(return_value=False),
        _mark_failed=AsyncMock(),
        _prepare_provider_recovery=AsyncMock(),
    )
    error = _PostAgentCommitStepError(
        stage="git commit",
        result=CommandResult(returncode=1, stdout="", stderr="boom"),
        classification=None,
    )

    await executor_quality_methods._mark_post_agent_commit_failed(  # noqa: SLF001
        executor,
        workspace_id="ws_term",
        error=error,
        agent_run_reason_code="AGENT_IDLE_TIMEOUT",
        agent_run_details={"provider": "openai", "model": "gpt-5"},
        agent_exit_note=None,
        upstream_failure_reason=FailureReason.agent_failure,
        execution_owner_id="worker-A",
    )

    executor.enter_recovering_for_provider_failure.assert_awaited_once()
    executor._mark_failed.assert_awaited_once()
    executor._prepare_provider_recovery.assert_awaited_once()
