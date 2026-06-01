"""Regression tests for PR monitor push outcome classification."""

from __future__ import annotations

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_CLEANUP_FAILED as REMOTE_OPS_VALIDATION_WORKTREE_CLEANUP_FAILED,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY as REMOTE_OPS_VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_STATUS_FAILED as REMOTE_OPS_VALIDATION_WORKTREE_STATUS_FAILED,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _append_git_recovery_failure,
    _git_failure_message,
    _git_push_failure_outcome,
    _GitPushResult,
)
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
)


def _make_push_result(reason_code: str) -> _GitPushResult:
    """Build a failure push-result payload for push-outcome mapping tests."""
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        reason_code=reason_code,
    )


@pytest.mark.parametrize(
    "reason_code",
    [
        "PRE_PUSH_VALIDATION_FAILED",
        "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED",
        "PRE_PUSH_VALIDATION_FIX_FAILED",
        "PRE_PUSH_VALIDATION_ROLLBACK_FAILED",
    ],
)
@pytest.mark.unit
def test_git_push_failure_outcome_maps_pre_push_validation_reasons(reason_code: str) -> None:
    """Pre-push validation reason codes should map to the monitor failure outcome."""
    assert _git_push_failure_outcome(_make_push_result(reason_code)) == "pre_push_validation_failed"


@pytest.mark.unit
def test_git_push_failure_outcome_maps_toolchain_missing_separately() -> None:
    assert (
        _git_push_failure_outcome(_make_push_result("PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"))
        == "pre_push_validation_toolchain_missing"
    )


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_rollback_failed_as_terminal() -> None:
    """Roll-back failure on pre-push validation should remain terminal."""
    assert _make_push_result("PRE_PUSH_VALIDATION_ROLLBACK_FAILED").terminal_monitor_failure is True


@pytest.mark.unit
def test_git_push_failure_outcome_defaults_to_git_push_failed() -> None:
    """Unknown push failures should retain the default push-failed outcome."""
    assert _git_push_failure_outcome(_make_push_result("UNKNOWN_FAILURE")) == "git_push_failed"


@pytest.mark.unit
def test_remote_ops_worktree_constants_match_validation_worktree() -> None:
    """Remote-op worktree reason codes should remain aligned with canonical constants."""
    assert REMOTE_OPS_VALIDATION_WORKTREE_CLEANUP_FAILED == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert (
        REMOTE_OPS_VALIDATION_WORKTREE_PRE_EXISTING_DIRTY == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    assert REMOTE_OPS_VALIDATION_WORKTREE_STATUS_FAILED == VALIDATION_WORKTREE_STATUS_FAILED
    assert (
        pre_push_validation.VALIDATION_WORKTREE_CLEANUP_FAILED == VALIDATION_WORKTREE_CLEANUP_FAILED
    )
    assert (
        pre_push_validation.VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    assert (
        pre_push_validation.VALIDATION_WORKTREE_STATUS_FAILED == VALIDATION_WORKTREE_STATUS_FAILED
    )


@pytest.mark.unit
def test_git_push_failure_outcome_maps_repair_and_protected_scope_reasons() -> None:
    """Monitor push-repair and workflow-scope outcomes map to their specific buckets."""
    assert (
        _git_push_failure_outcome(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            )
        )
        == "protected_scope_diff_unavailable"
    )
    assert (
        _git_push_failure_outcome(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            )
        )
        == "protected_scope_push_blocked"
    )
    assert _git_push_failure_outcome(_make_push_result("REPAIR_WORKTREE_STATUS_FAILED")) == (
        "repair_start_blocked"
    )


@pytest.mark.unit
def test_git_failure_message_prefers_stderr_then_stdout() -> None:
    """Failure messages should prefer stderr over stdout when formatting command output."""
    assert (
        _git_failure_message(
            "git push",
            CommandResult(returncode=128, stdout="", stderr=" denied \n"),
        )
        == "git push failed with exit code 128; stderr: denied"
    )
    assert (
        _git_failure_message(
            "git fetch",
            CommandResult(returncode=1, stdout=" retried \n", stderr=""),
        )
        == "git fetch failed with exit code 1; stdout: retried"
    )
    assert (
        _git_failure_message(
            "git status",
            CommandResult(returncode=2, stdout="", stderr=""),
        )
        == "git status failed with exit code 2"
    )


@pytest.mark.unit
def test_append_git_recovery_failure_includes_available_context() -> None:
    """Recovery failure messages should include upstream and fallback operation context."""
    assert _append_git_recovery_failure(
        push_stderr="push rejected",
        recovery_stderr="fetch failed",
        operation="fetch",
    ) == (
        "push rejected\n"
        "AWF worktree recovery failed during git push failure resync "
        "(fetch failed: fetch failed)"
    )
    assert (
        _append_git_recovery_failure(
            push_stderr=None,
            recovery_stderr=None,
            operation="reset",
        )
        == "AWF worktree recovery failed during git push failure resync (reset failed)"
    )
