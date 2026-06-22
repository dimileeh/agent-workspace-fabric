"""``decide()`` coverage for the operator ``require_ci`` opt-out (#469).

A Bitbucket (or any) repo that runs NO CI reports ``check_state=PENDING`` with an
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
    NotifyHuman,
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
    awaiting_required_checks_grace_active: bool = False,
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
        awaiting_required_checks_grace_active=awaiting_required_checks_grace_active,
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


# ── #655: bounded grace before HUMAN_WAIT on transient required-CI absence ──
#
# Right after a monitor push, the new head has NO checks yet
# (``no_checks_observed=True``) and branch protection reports
# ``merge_state_status == BLOCKED`` because the required ``ci-required`` context
# is absent. The empty rollup parses to a non-PENDING ``check_state`` (SUCCESS /
# NEUTRAL), so gate 6 misses it and ``decide`` used to fall straight through to
# gate 9 (``BLOCKED → NotifyHuman``) — a premature human ping that the monitor
# self-recovers from once CI lands. The new gate defers to a bounded
# ``WaitForCI`` while the runner-set grace flag is active, escalating only after
# the grace expires. The safety hinge: it fires ONLY when checks are ABSENT, so a
# genuine human-gate block (checks present + BLOCKED) still escalates immediately.


@pytest.mark.unit
def test_within_grace_absent_ci_blocked_waits() -> None:
    # A.1: within-grace, required CI absent, BLOCKED → bounded WaitForCI.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "awaiting_required_checks"


@pytest.mark.unit
def test_grace_expired_absent_ci_blocked_notifies_human() -> None:
    # A.2: grace expired (flag False) → gate 9 escalates as before.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=False,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
def test_within_grace_absent_ci_has_hooks_waits() -> None:
    # A.3: HAS_HOOKS is the other member of the merge-state tuple — also deferred.
    action = decide(
        status=_status(
            check_state=CheckState.NEUTRAL,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.HAS_HOOKS,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "awaiting_required_checks"


@pytest.mark.unit
def test_checks_present_human_gate_notifies_human_immediately() -> None:
    # A.4 (safety hinge): checks PRESENT but BLOCKED on a real human gate
    # (no_checks_observed=False). The grace flag is irrelevant — the new gate is
    # skipped and gate 9 escalates immediately, even within the window.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=False,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
def test_require_ci_false_absent_ci_blocked_unchanged() -> None:
    # A.5: require_ci=False + absent checks + BLOCKED → the new gate is skipped
    # (require_ci is False) and gate 9 escalates exactly as before.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(require_ci=False),
    )
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
def test_pending_checks_still_routes_to_gate_6() -> None:
    # A.6: ordering preserved — the GENUINE-PENDING shape (real checks present
    # and running, ``no_checks_observed=False``) still fires gate 6 before the
    # required-CI grace gate, even with the grace flag set and a BLOCKED merge
    # state. This pins the #469 contract that genuinely pending required checks
    # wait forever at gate 6. The ABSENT-PENDING shape
    # (``no_checks_observed=True``) is covered separately by the
    # #660 absent-PENDING tests below — it no longer parks at gate 6.
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=False,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "pending_checks"


# ── #660: route the ABSENT-PENDING shape through the required-CI grace ──
#
# The null-rollup shape reports ``check_state == PENDING`` together with
# ``no_checks_observed == True``. Gate 6 used to intercept this under
# ``require_ci=True`` and poll forever, defeating #656's "a head that
# genuinely never gets CI still surfaces HUMAN_WAIT after the grace"
# guarantee. The fix carves ONLY the ABSENT-PENDING + BLOCKED/HAS_HOOKS shape
# out of gate 6 so it falls through to gate 8b (grace active → bounded
# WaitForCI; grace expired → gate 9 → NotifyHuman). The GENUINE-PENDING shape
# (A.6 above) is untouched.


@pytest.mark.unit
def test_absent_pending_grace_expired_routes_to_notify_human() -> None:
    # The bug fix: ABSENT-PENDING (null rollup) + BLOCKED + require_ci, grace
    # EXPIRED → NotifyHuman (was WaitForCI("pending_checks") forever).
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=False,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
def test_absent_pending_within_grace_routes_to_awaiting_required_checks() -> None:
    # Same ABSENT-PENDING shape, grace ACTIVE → bounded WaitForCI within the
    # required-CI grace window (gate 8b), not the gate-6 forever wait.
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "awaiting_required_checks"


@pytest.mark.unit
def test_absent_pending_has_hooks_grace_expired_routes_to_notify_human() -> None:
    # HAS_HOOKS variant — pins that the carve-out covers both members of the
    # merge-state tuple gate 8b matches (mirrors A.3 for the SUCCESS/NEUTRAL
    # shape).
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.HAS_HOOKS,
            awaiting_required_checks_grace_active=False,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
def test_absent_pending_clean_state_still_waits_at_gate_6() -> None:
    # Narrowness pin: absent-PENDING on a CLEAN state does NOT escape gate 6 —
    # no grace gate would catch it, and we must not merge a PENDING head blind.
    # The carve-out is scoped to BLOCKED/HAS_HOOKS only.
    action = decide(
        status=_status(
            check_state=CheckState.PENDING,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.CLEAN,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "pending_checks"


@pytest.mark.unit
def test_failure_still_routes_to_gate_5() -> None:
    # A.7: FAILURE fires gate 5 (ReportCiFailure); the new gate must not steal it
    # even within grace with checks absent and BLOCKED.
    action = decide(
        status=_status(
            check_state=CheckState.FAILURE,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.BLOCKED,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, ReportCiFailure)


@pytest.mark.unit
def test_unknown_merge_state_still_routes_to_gate_6() -> None:
    # A.8: UNKNOWN mergeable/merge_state hits gate 6's unknown branch before the
    # new gate, which only matches BLOCKED/HAS_HOOKS.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.UNKNOWN,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, WaitForCI)
    assert action.reason == "unknown_mergeable_state"


@pytest.mark.unit
def test_grace_flag_does_not_block_clean_green_merge() -> None:
    # A.9 (regression): the grace flag only matters on BLOCKED/HAS_HOOKS. A clean,
    # all-green PR still merges even when the flag is left True.
    action = decide(
        status=_status(
            check_state=CheckState.SUCCESS,
            no_checks_observed=True,
            merge_state_status=MergeStateStatus.CLEAN,
            awaiting_required_checks_grace_active=True,
        ),
        state=MonitorState(),
        config=MonitorConfig(),
    )
    assert isinstance(action, Merge)
