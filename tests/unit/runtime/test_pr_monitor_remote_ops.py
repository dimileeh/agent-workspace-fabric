"""Regression tests for PR monitor push outcome classification."""

from __future__ import annotations

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.remote_ops import (
    _append_git_recovery_failure,
    _git_failure_message,
    _git_push_failure_outcome,
    _GitPushResult,
)


def _make_push_result(reason_code: str) -> _GitPushResult:
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
    ],
)
@pytest.mark.unit
def test_git_push_failure_outcome_maps_pre_push_validation_reasons(reason_code: str) -> None:
    assert _git_push_failure_outcome(_make_push_result(reason_code)) == "pre_push_validation_failed"


@pytest.mark.unit
def test_git_push_failure_outcome_maps_toolchain_missing_separately() -> None:
    assert (
        _git_push_failure_outcome(_make_push_result("PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"))
        == "pre_push_validation_toolchain_missing"
    )


@pytest.mark.unit
def test_git_push_failure_outcome_defaults_to_git_push_failed() -> None:
    assert _git_push_failure_outcome(_make_push_result("UNKNOWN_FAILURE")) == "git_push_failed"


@pytest.mark.unit
def test_git_push_failure_outcome_maps_repair_and_protected_scope_reasons() -> None:
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
