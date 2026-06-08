"""``decide()`` coverage for the operator ``require_ci`` opt-out (#469).

A BitBucket (or any) repo that runs NO CI reports ``check_state=PENDING`` with an
empty status set forever, so the default ``require_ci=True`` keeps the monitor in
``WaitForCI("pending_checks")``. When an operator sets ``require_ci=False`` AND the
forge authoritatively reports an empty check set (``no_checks_observed=True``),
``decide()`` skips that gate and a clean PR reaches ``Merge()``.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
    ReportCiFailure,
    WaitForCI,
    decide,
)


def _status(
    *,
    check_state: CheckState = CheckState.PENDING,
    no_checks_observed: bool = False,
    mergeable: MergeableState = MergeableState.MERGEABLE,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    ci_failures: tuple[CheckFailure, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=7,
        head_sha="abc123",
        mergeable=mergeable,
        check_state=check_state,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=merge_state_status,
        ci_failures=ci_failures,
        no_checks_observed=no_checks_observed,
    )


@pytest.mark.unit
def test_require_ci_false_no_checks_clean_pr_merges() -> None:
    # Proves the end-to-end no-CI path (#469 point 7): gate 6 is bypassed and a
    # clean MERGEABLE/CLEAN PR reaches the terminal Merge() action.
    action = decide(
        status=_status(check_state=CheckState.PENDING, no_checks_observed=True),
        state=MonitorState(),
        config=MonitorConfig(require_ci=False),
    )
    assert isinstance(action, Merge)


@pytest.mark.unit
def test_require_ci_false_no_checks_unknown_mergeable_waits() -> None:
    # After the check gate is bypassed, an UNKNOWN mergeable state still parks the
    # monitor on the unknown_mergeable_state gate rather than merging blind.
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=True,
            mergeable=MergeableState.UNKNOWN,
        ),
        state=MonitorState(),
        config=MonitorConfig(require_ci=False),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "unknown_mergeable_state"


@pytest.mark.unit
def test_require_ci_false_with_checks_present_still_waits() -> None:
    # Signal off (checks exist but are pending) ⇒ never skip, even with the
    # opt-out enabled. The safe-default invariant guards a real-CI repo.
    action = decide(
        status=_status(check_state=CheckState.PENDING, no_checks_observed=False),
        state=MonitorState(),
        config=MonitorConfig(require_ci=False),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "pending_checks"


@pytest.mark.unit
def test_require_ci_false_failure_still_reports() -> None:
    # The failure path is unaffected by the opt-out.
    action = decide(
        status=_status(check_state=CheckState.FAILURE, no_checks_observed=True),
        state=MonitorState(),
        config=MonitorConfig(require_ci=False),
    )
    assert isinstance(action, ReportCiFailure)


@pytest.mark.unit
def test_require_ci_true_default_no_checks_waits_regression() -> None:
    # REGRESSION: today's behavior is preserved — a no-CI repo with the default
    # require_ci=True keeps waiting on pending_checks forever.
    config = MonitorConfig()
    assert config.require_ci is True
    action = decide(
        status=_status(check_state=CheckState.PENDING, no_checks_observed=True),
        state=MonitorState(),
        config=config,
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "pending_checks"
