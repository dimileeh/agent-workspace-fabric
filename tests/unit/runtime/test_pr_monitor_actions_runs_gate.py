"""``decide`` never certifies while a GitHub Actions run for the head is unsettled (#1230)."""

from __future__ import annotations

from dataclasses import replace

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
from awf.runtime.pr_monitor_models import ActionsRunsState
from tests.unit.runtime.test_pr_monitor import _status


def _green(actions_runs: ActionsRunsState, **overrides: object) -> PRStatus:
    return replace(_status(), actions_runs=actions_runs, **overrides)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("auto_merge", [True, False])
def test_green_rollup_with_queued_actions_run_waits_instead_of_certifying(
    auto_merge: bool,
) -> None:
    action = decide(
        _green(ActionsRunsState.PENDING), MonitorState(), MonitorConfig(auto_merge=auto_merge)
    )

    assert action == WaitForCI(reason="actions_runs_pending")


@pytest.mark.unit
def test_unknown_actions_runs_waits_with_its_own_reason() -> None:
    action = decide(_green(ActionsRunsState.UNKNOWN), MonitorState(), MonitorConfig())

    assert action == WaitForCI(reason="actions_runs_unknown")


@pytest.mark.unit
def test_forgotten_populate_defaults_to_wait() -> None:
    status = PRStatus(
        number=1,
        head_sha="abc",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    assert status.actions_runs is ActionsRunsState.UNKNOWN
    assert decide(status, MonitorState(), MonitorConfig(auto_merge=True)) == WaitForCI(
        reason="actions_runs_unknown"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("auto_merge", "expected"),
    [(True, Merge()), (False, NotifyHuman())],
)
def test_settled_actions_runs_keep_the_existing_ready_path(
    auto_merge: bool, expected: object
) -> None:
    action = decide(
        _green(ActionsRunsState.SETTLED), MonitorState(), MonitorConfig(auto_merge=auto_merge)
    )

    assert action == expected


@pytest.mark.unit
def test_no_rollup_without_required_ci_still_waits_for_pending_actions_runs() -> None:
    status = _green(
        ActionsRunsState.PENDING, check_state=CheckState.PENDING, no_checks_observed=True
    )

    action = decide(status, MonitorState(), MonitorConfig(require_ci=False))

    assert action == WaitForCI(reason="actions_runs_pending")


@pytest.mark.unit
def test_failure_path_is_unchanged_by_pending_actions_runs() -> None:
    failure = CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="boom")
    status = _green(
        ActionsRunsState.PENDING, check_state=CheckState.FAILURE, ci_failures=(failure,)
    )

    assert isinstance(decide(status, MonitorState(), MonitorConfig()), ReportCiFailure)
    in_progress = replace(status, ci_failures=(), ci_runs_in_progress=True)
    assert decide(in_progress, MonitorState(), MonitorConfig()) == WaitForCI(
        reason="ci_run_in_progress"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"check_state": CheckState.PENDING}, WaitForCI(reason="pending_checks")),
        (
            {"merge_state_status": MergeStateStatus.UNKNOWN},
            WaitForCI(reason="unknown_mergeable_state"),
        ),
        ({"merge_state_status": MergeStateStatus.BLOCKED}, NotifyHuman()),
    ],
)
def test_earlier_gates_still_win_over_the_actions_runs_gate(
    overrides: dict[str, object], expected: object
) -> None:
    action = decide(_green(ActionsRunsState.PENDING, **overrides), MonitorState(), MonitorConfig())

    assert action == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (ActionsRunsState.UNKNOWN, "actions_runs_unknown"),
        (ActionsRunsState.PENDING, "actions_runs_pending"),
        (ActionsRunsState.SETTLED, None),
    ],
)
def test_actions_runs_wait_reason_mapping(state: ActionsRunsState, reason: str | None) -> None:
    assert _green(state).actions_runs_wait_reason == reason
