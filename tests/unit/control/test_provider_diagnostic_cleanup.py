"""Diagnostic disposal removes owned resources only after runtime quiescence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.worker.cleanup import _release_terminal_runtime_for_candidate
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.node.cleanup import WorkspaceCleaner, WorkspaceCleanupResult
from awf.node.compose_manager import ComposeOperationError


@pytest.mark.parametrize("ok", [False, True])
async def test_worker_diagnostics_are_disposable_and_results_outlive_fixture(monkeypatch, ok):
    from awf.node.cleanup import WorkspaceCleanupStepResult

    result = (
        WorkspaceCleanupResult.skipped()
        if ok
        else WorkspaceCleanupResult.from_steps(
            [WorkspaceCleanupStepResult("compose_down", "failed", "FAILED")]
        )
    )
    worker = SimpleNamespace(
        _runtime_cleaner=SimpleNamespace(cleanup=AsyncMock(return_value=result)),
        _record_terminal_runtime_released=AsyncMock(),
        _record_terminal_runtime_release_failed=AsyncMock(),
    )
    monkeypatch.setattr(
        "awf.control.worker.cleanup._teardown_terminal_auth_overlay", AsyncMock(return_value=None)
    )
    candidate = _TerminalRuntimeCandidate(
        "ws_diag",
        WorkspaceStatus.failed,
        "awf_ws_diag",
        None,
        "awf-internal://provider-diagnostic/v1",
        task_policy={"provider_diagnostic": {"result": {"outcome": "inconclusive"}}},
    )
    await _release_terminal_runtime_for_candidate(worker, candidate)
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_volumes"] is True
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_worktree"] is True
    assert candidate.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
    assert worker._record_terminal_runtime_released.await_count == int(ok)
    assert worker._record_terminal_runtime_release_failed.await_count == int(not ok)


async def test_failed_teardown_retains_diagnostic_fixture(tmp_path):
    error = ComposeOperationError(
        operation="down", returncode=1, stdout="", stderr="cannot stop", reason_code="DOWN_FAILED"
    )
    compose = SimpleNamespace(
        down=AsyncMock(side_effect=error), remove_project_by_label=AsyncMock(side_effect=error)
    )
    git = SimpleNamespace(remove_worktree=AsyncMock(), _worktrees_dir=tmp_path)
    result = await WorkspaceCleaner(git=git, compose=compose).cleanup(
        workspace_id="ws_diag",
        repo_url="awf-internal://provider-diagnostic/v1",
        remove_worktree=True,
    )
    assert not result.ok
    git.remove_worktree.assert_not_awaited()
