"""Regression tests for PR monitor push outcome classification."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome, _GitPushResult


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
        "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING",
    ],
)
@pytest.mark.unit
def test_git_push_failure_outcome_maps_pre_push_validation_reasons(reason_code: str) -> None:
    assert _git_push_failure_outcome(_make_push_result(reason_code)) == "pre_push_validation_failed"


@pytest.mark.unit
def test_git_push_failure_outcome_defaults_to_git_push_failed() -> None:
    assert _git_push_failure_outcome(_make_push_result("UNKNOWN_FAILURE")) == "git_push_failed"
