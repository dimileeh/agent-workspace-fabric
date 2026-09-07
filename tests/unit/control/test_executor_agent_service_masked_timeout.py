"""Masked-timeout classification in agent compose-service recovery.

A compose cleanup failure can mask the watchdog timeout that actually ended the
run. These tests pin that the recorded ``source_reason_code`` stays the timeout
rather than the cleanup mask, and that an untagged cleanup failure keeps its own
classification.

Kept in a sibling module so ``test_executor_agent_service_recovery`` stays under
the first-party line budget; the shared fixtures are imported from there.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.executor import agent_service_recovery
from tests.unit.control.test_executor_agent_service_recovery import (
    _cleanup_error,
    _executor,
    _run_helper,
)


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", ["AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"])
async def test_cleanup_repair_failure_records_masked_timeout_classification(
    reason_code: str,
) -> None:
    executor = SimpleNamespace(_mark_failed=AsyncMock())
    recover_missing_head_after_cleanup_failure = AsyncMock(return_value=False)

    result = await agent_service_recovery._repair_after_recoverable_agent_cleanup_failure(
        executor,
        _cleanup_error(agent_reason_code=reason_code),
        workspace_id="ws_agent_service",
        owned_paths=["src/awf"],
        execution_owner_id="worker-1",
        repair_hooks_after_agent_cleanup_failure=AsyncMock(return_value=True),
        recover_missing_head_after_cleanup_failure=recover_missing_head_after_cleanup_failure,
        deposit_planning_artifacts=lambda: None,
    )

    assert result == "EXEC_PROCESS_CLEANUP_FAILED"
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["reason_code"] == "EXEC_PROCESS_CLEANUP_FAILED"
    assert mark_failed_kwargs["details"]["agent_service_recovery"] == {
        "reason_code": "EXEC_PROCESS_CLEANUP_FAILED",
        "source_reason_code": reason_code,
    }


@pytest.mark.unit
@pytest.mark.parametrize("agent_reason_code", [None, "AGENT_CLI_FAILED"])
async def test_cleanup_repair_failure_omits_details_without_masked_timeout(
    agent_reason_code: str | None,
) -> None:
    executor = SimpleNamespace(_mark_failed=AsyncMock())

    result = await agent_service_recovery._repair_after_recoverable_agent_cleanup_failure(
        executor,
        _cleanup_error(agent_reason_code=agent_reason_code),
        workspace_id="ws_agent_service",
        owned_paths=["src/awf"],
        execution_owner_id="worker-1",
        repair_hooks_after_agent_cleanup_failure=AsyncMock(return_value=True),
        recover_missing_head_after_cleanup_failure=AsyncMock(return_value=False),
        deposit_planning_artifacts=lambda: None,
    )

    assert result == "EXEC_PROCESS_CLEANUP_FAILED"
    assert "details" not in executor._mark_failed.await_args.kwargs


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", ["AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT"])
async def test_masked_timeout_cleanup_failure_keeps_timeout_source_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason_code: str,
) -> None:
    """A cleanup failure masking a watchdog timeout records the timeout, not the mask."""
    executor = _executor(
        side_effect=[
            _cleanup_error(agent_reason_code=reason_code),
            _cleanup_error(agent_reason_code=reason_code),
            _cleanup_error(agent_reason_code=reason_code),
        ]
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is False
    assert planning_failure is None
    executor._mark_failed.assert_awaited_once()
    recovery_details = executor._mark_failed.await_args.kwargs["details"]["agent_service_recovery"]
    assert recovery_details["source_reason_code"] == reason_code


@pytest.mark.unit
async def test_untagged_cleanup_failure_keeps_cleanup_source_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cleanup failure with no masked timeout keeps its own classification."""
    executor = _executor(
        side_effect=[_cleanup_error(), _cleanup_error(), _cleanup_error()],
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is False
    assert planning_failure is None
    executor._mark_failed.assert_awaited_once()
    recovery_details = executor._mark_failed.await_args.kwargs["details"]["agent_service_recovery"]
    assert recovery_details["source_reason_code"] == "EXEC_PROCESS_CLEANUP_FAILED"
