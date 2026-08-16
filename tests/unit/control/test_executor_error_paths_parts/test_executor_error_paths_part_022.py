"""Hosted monitor handoff setup failure edge tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.control.executor.monitor_handoff_setup import (
    _MonitorHandoffSetupFailureError,
    _run_hosted_monitor_handoff_profile_setup,
)
from awf.db.enums import FailureReason
from awf.runtime.validation import ValidationResult


class _ExplodingSetupValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: object,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("setup", "pre_agent"):
            raise RuntimeError("setup failed with ghp_FAKESECRET0000000")
        return ValidationResult()


@pytest.mark.unit
async def test_hosted_monitor_handoff_profile_setup_exception_marks_failed_with_redacted_reason(
    tmp_path: Path,
) -> None:
    """Unexpected hosted setup exceptions should fail closed without leaking secrets."""
    validation = _ExplodingSetupValidation()
    mark_failed_calls: list[dict[str, Any]] = []

    class _Executor:
        _hosted_validation = validation

        async def _mark_failed(self, **kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)

    ok = await _run_hosted_monitor_handoff_profile_setup(
        _Executor(),
        workspace_id="ws-hosted",
        profile=object(),
        compose_project="awf_x",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        pr_identity={"pr_number": 42},
    )

    assert ok is False
    assert validation.calls == [("setup", "pre_agent")]
    assert len(mark_failed_calls) == 1
    failure = mark_failed_calls[0]
    assert failure["failure_reason"] == FailureReason.infrastructure_failure
    assert failure["reason_code"] == PR_MONITOR_SETUP_FAILED_REASON_CODE
    assert "hosted monitor handoff profile setup failed" in failure["message"]
    assert "ghp_FAKESECRET0000000" not in failure["message"]


@pytest.mark.unit
async def test_hosted_monitor_handoff_profile_setup_reraises_classified_setup_failure(
    tmp_path: Path,
) -> None:
    """Hosted setup should not wrap failures already classified by AWF."""
    mark_failed_calls: list[dict[str, Any]] = []

    class _Validation:
        async def run_profile_phases(self, **_kwargs: Any) -> ValidationResult:
            raise _MonitorHandoffSetupFailureError(
                failure_reason=FailureReason.infrastructure_failure,
                message="classified hosted setup failure",
                reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            )

    class _Executor:
        _hosted_validation = _Validation()

        async def _mark_failed(self, **kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)

    with pytest.raises(
        _MonitorHandoffSetupFailureError,
        match="classified hosted setup failure",
    ):
        await _run_hosted_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-hosted",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path / "worktree",
            pr_identity={"pr_number": 42},
        )

    assert mark_failed_calls == []
