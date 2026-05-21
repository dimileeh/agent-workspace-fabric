# PRRT_kwDOSJAM6s6Djxn7 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Djxn7_PLAN.md`

## Requirement Status

- Complete: Added a regression test covering preserved active PR-monitor
  attachment from both `validating` and `pushing` with pending/running active
  validate/push operation rows.
- Complete: `_attach_preserved_active_pr_monitor` now cancels superseded active
  validate/push operations before transitioning non-running candidates to
  `monitoring_pr`.
- Complete: Running-workspace monitor attachment behavior is preserved because
  cancellation only runs when `candidate.status != WorkspaceStatus.running`.
- Complete: The monitor-attach salvage payload records
  `cancelled_active_operations`, and cancelled operations record the monitor
  attach reason code with `requested_action=remonitor`.
- Complete: Changes are scoped to worker recovery logic, focused worker tests,
  and the required plan/validation docs.

## Evidence

- Changed files:
  - `src/awf/control/worker.py`
  - `tests/unit/control/test_worker.py`
  - `plans/PRRT_kwDOSJAM6s6Djxn7_PLAN.md`
  - `plans/PRRT_kwDOSJAM6s6Djxn7_VALIDATION.md`
- Confirmed the regression failed before the fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_pr_handoff_cancels_superseded_active_operations`
  - Result before fix: 2 failures; original operations remained `pending` and
    `running`.
- Verification after fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_pr_handoff_cancels_superseded_active_operations`
  - Result: 2 passed, 235 deselected.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_pr_handoff or preserved_active_pushed_branch_open_pr"`
  - Result: 8 passed, 229 deselected.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_validation_salvage_cancels_prior_cycle_salvage_operation"`
  - Result: 3 passed, 234 deselected.
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: all checks passed.

## Remaining Gaps

None.
