"""Executor hosted PR-adoption setup validation coverage. (split part)"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _seed_ready,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_017 import (
    _PR_ADOPTION_POLICY,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_018 import (
    _ExplodingSetupValidation,
    _HostedSetupValidation,
)

_IMPORTED_FIXTURES = (factory, fake)


class TestExecutorHostedPrAdoptionSetupMissingValidation:
    async def test_hosted_pr_adoption_delegates_baseline_coverage_with_pr_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Hosted baseline coverage must not execute against the absent local stack."""

        monkeypatch.setattr(
            "awf.control.executor.execution_flow.repair_agent_runtime_ownership",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "awf.control.executor.execution_flow.repair_mirror_hooks_path_or_mark_failed",
            AsyncMock(return_value=True),
        )
        hosted_policy = {
            "pr_adoption": {
                **_PR_ADOPTION_POLICY["pr_adoption"],
                "execution": {"mode": "hosted"},
            }
        }
        local_validation = _ExplodingSetupValidation()
        hosted_validation = _HostedSetupValidation([])
        ws_id = await _seed_ready(factory, task_policy=hosted_policy)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=local_validation,
            hosted_validation=hosted_validation,
        )
        baseline_calls: list[dict[str, Any]] = []

        async def _measure_baseline(**kwargs: Any) -> None:
            baseline_calls.append(kwargs)

        async def _recheck_status(
            _workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> bool:
            assert expected == WorkspaceStatus.running
            return action in {"execute", "baseline_coverage_preflight"}

        monkeypatch.setattr(executor, "_measure_and_persist_baseline_coverage", _measure_baseline)
        monkeypatch.setattr(
            executor,
            "_run_agent_git_writability_preflight",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            executor,
            "_ensure_ollama_model_or_mark_failed",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.execute(ws_id)

        assert local_validation.calls == []
        assert hosted_validation.calls == [("setup", "pre_agent")]
        assert len(baseline_calls) == 1
        assert baseline_calls[0]["coverage_runner"] is hosted_validation
        identity = baseline_calls[0]["coverage_run_kwargs"]["pr_identity"]
        assert identity["pr_number"] == 42
        assert identity["head_ref"] == "awf/x"
        assert baseline_calls[0]["worktree_path"] == _test_worktrees_root(factory) / ws_id

    async def test_initial_hosted_pr_adoption_setup_missing_hosted_validation_is_not_recovery_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Initial hosted setup must not look like monitor-recovery setup."""

        async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(
            "awf.control.executor.execution_flow.repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )
        hosted_policy = {
            "pr_adoption": {
                **_PR_ADOPTION_POLICY["pr_adoption"],
                "execution": {"mode": "hosted"},
            }
        }
        local_validation = _ExplodingSetupValidation()
        ws_id = await _seed_ready(factory, task_policy=hosted_policy)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=local_validation,
        )

        await executor.execute(ws_id)

        assert local_validation.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert "no hosted validation runner configured" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == PR_MONITOR_SETUP_FAILED_REASON_CODE

    async def test_recovery_hosted_pr_adoption_setup_missing_hosted_validation_keeps_recovery_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Monitor recovery setup still uses the monitor-recovery reason."""

        async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(
            "awf.control.executor.execution_flow.repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )
        hosted_policy = {
            "pr_adoption": {
                **_PR_ADOPTION_POLICY["pr_adoption"],
                "execution": {"mode": "hosted"},
            }
        }
        ws_id = await _seed_ready(factory, task_policy=hosted_policy)
        async with factory() as s:
            await OperationRepository(s).create(
                workspace_id=ws_id,
                operation_type=OperationType.validate,
                payload={
                    "source": "pr_monitor",
                    "recovery_mode": "validate_only",
                    "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                },
                idempotency_key=f"pr_monitor:validate_only:{ws_id}",
            )
            await s.commit()
        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.events[-1].reason_code == "MONITOR_RECOVERY_SETUP_FAILED"
        recovery_op = next(
            op
            for op in ops
            if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
        )
        assert recovery_op.status == OperationStatus.failed.value
        assert recovery_op.error_code == "MONITOR_RECOVERY_SETUP_FAILED"
        assert recovery_op.result == {"reason_code": "MONITOR_RECOVERY_SETUP_FAILED"}
