"""Regression tests for terminal pre-push validation push outcomes."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


@pytest.mark.unit
def test_toolchain_missing_pre_push_validation_failure_is_terminal() -> None:
    result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=127,
        reason_code="PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING",
    )

    assert result.terminal_monitor_failure is True
