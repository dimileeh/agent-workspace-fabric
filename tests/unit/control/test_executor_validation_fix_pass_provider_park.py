"""Executor validation fix-pass managed-key provider-park tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.run_results import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import execution_validation_fix_pass as execution_validation_fix_pass_mod
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit._validation_git_probes import answer_validation_git_probes
from tests.unit.control.test_executor_validation_fix_cycle import (
    _make_executor,
    _queue_initial_pass,
    _seed_ready_workspace,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async SQLAlchemy session factory for validation-cycle tests."""
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    """Create a fake command runner for subprocess assertions."""
    fake = FakeCommandRunner()
    answer_validation_git_probes(fake)
    return fake


class TestFixPassProviderPark:
    """A managed-key park code from a fix-pass agent parks instead of looping."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason_code", ["PROVIDER_BUDGET_EXHAUSTED", "PROVIDER_MANAGED_KEY_UNAVAILABLE"]
    )
    async def test_fix_pass_park_code_parks_the_workspace_without_more_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        reason_code: str,
    ) -> None:
        """Later passes could only fail to mint, then fail as a validation failure."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=3)
        ws_id = await _seed_ready_workspace(factory)
        fix_runs: list[object] = []

        async def _budget_exhausted(*_args: object, **_kwargs: object) -> tuple[bool, object]:
            fix_runs.append(object())
            raise AgentRunError(
                agent="codex",
                result=CommandResult(returncode=78, stdout="", stderr=reason_code),
                reason_code=reason_code,
                details={"provider_budget": {"budget_usd": "1.000000"}, "retryable": False},
            )

        monkeypatch.setattr(
            execution_validation_fix_pass_mod,
            "_run_agent_callable_with_service_recovery",
            _budget_exhausted,
        )
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # validation fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.blocked.value
            assert ws.block_type == "provider_budget"
            assert ws.block_reason_code == reason_code
        assert len(fix_runs) == 1

    @pytest.mark.unit
    async def test_fix_pass_park_code_from_superseded_executor_leaves_new_claim_validating(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A superseded executor's managed-key stop during a fix pass cannot park
        (or fail) the replacement claimant's ``validating`` row
        (PRRT_kwDOSJAM6s6157hr)."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=3)
        ws_id = await _seed_ready_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.execution_claimed_by = "old-worker"
            await s.commit()

        async def _claim_lost_then_budget_exhausted(
            *_args: object, **_kwargs: object
        ) -> tuple[bool, object]:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.execution_claimed_by = "new-worker"
                await s.commit()
            raise AgentRunError(
                agent="codex",
                result=CommandResult(returncode=78, stdout="", stderr="PROVIDER_BUDGET_EXHAUSTED"),
                reason_code="PROVIDER_BUDGET_EXHAUSTED",
                details={"retryable": False},
            )

        monkeypatch.setattr(
            execution_validation_fix_pass_mod,
            "_run_agent_callable_with_service_recovery",
            _claim_lost_then_budget_exhausted,
        )
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # validation fails

        await executor.execute(ws_id, execution_owner_id="old-worker")

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.validating.value
            assert ws.execution_claimed_by == "new-worker"
            assert ws.block_type is None
            assert ws.failure_reason is None

    @pytest.mark.unit
    async def test_fix_pass_park_code_from_superseded_executor_leaves_validate_ops_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A superseded executor's fix-pass park must not fail the replacement
        claimant's pending validate operations before its fenced park no-ops
        (PRRT_kwDOSJAM6s616TNZ)."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=3)
        ws_id = await _seed_ready_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.execution_claimed_by = "old-worker"
            await s.commit()

        async def _claim_lost_then_budget_exhausted(
            *_args: object, **_kwargs: object
        ) -> tuple[bool, object]:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.execution_claimed_by = "new-worker"
                s.add(
                    Operation(
                        id="op_new_claim_validate",
                        workspace_id=ws_id,
                        type=OperationType.validate.value,
                        status=OperationStatus.pending.value,
                        payload={},
                    )
                )
                await s.commit()
            raise AgentRunError(
                agent="codex",
                result=CommandResult(returncode=78, stdout="", stderr="PROVIDER_BUDGET_EXHAUSTED"),
                reason_code="PROVIDER_BUDGET_EXHAUSTED",
                details={"retryable": False},
            )

        monkeypatch.setattr(
            execution_validation_fix_pass_mod,
            "_run_agent_callable_with_service_recovery",
            _claim_lost_then_budget_exhausted,
        )
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # validation fails

        await executor.execute(ws_id, execution_owner_id="old-worker")

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.validating.value
            assert ws.execution_claimed_by == "new-worker"
            op = await s.get(Operation, "op_new_claim_validate")
            assert op is not None
            assert op.status == OperationStatus.pending.value
            assert op.error_code is None

    @pytest.mark.unit
    async def test_fix_pass_park_claim_lost_after_recheck_leaves_validate_ops_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The validate-op finish re-checks the execution owner under the row lock,
        so a claim that moves after the park's status recheck still leaves the
        replacement claimant's validate operations pending (PRRT_kwDOSJAM6s616TNZ)."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=3)
        ws_id = await _seed_ready_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.execution_claimed_by = "old-worker"
            await s.commit()

        original_recheck = executor._recheck_status

        async def _recheck_then_lose_claim(workspace_id: str, **kwargs: Any) -> bool:
            ok = await original_recheck(workspace_id, **kwargs)
            if kwargs.get("action") == "validation_fix_provider_park":
                async with factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    ws.execution_claimed_by = "new-worker"
                    s.add(
                        Operation(
                            id="op_post_recheck_validate",
                            workspace_id=workspace_id,
                            type=OperationType.validate.value,
                            status=OperationStatus.pending.value,
                            payload={},
                        )
                    )
                    await s.commit()
            return ok

        monkeypatch.setattr(executor, "_recheck_status", _recheck_then_lose_claim)

        async def _budget_exhausted(*_args: object, **_kwargs: object) -> tuple[bool, object]:
            raise AgentRunError(
                agent="codex",
                result=CommandResult(returncode=78, stdout="", stderr="PROVIDER_BUDGET_EXHAUSTED"),
                reason_code="PROVIDER_BUDGET_EXHAUSTED",
                details={"retryable": False},
            )

        monkeypatch.setattr(
            execution_validation_fix_pass_mod,
            "_run_agent_callable_with_service_recovery",
            _budget_exhausted,
        )
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # validation fails

        await executor.execute(ws_id, execution_owner_id="old-worker")

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.validating.value
            assert ws.execution_claimed_by == "new-worker"
            assert ws.block_type is None
            op = await s.get(Operation, "op_post_recheck_validate")
            assert op is not None
            assert op.status == OperationStatus.pending.value
            assert op.error_code is None
