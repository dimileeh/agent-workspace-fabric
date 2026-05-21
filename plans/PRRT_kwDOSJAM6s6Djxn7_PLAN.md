# PRRT_kwDOSJAM6s6Djxn7 Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Djxn7` reports that preserved active execution
recovery can attach a PR monitor from a `validating` or `pushing` workspace
without cancelling the stale active validate/push operation left behind by the
interrupted executor. The workspace moves to `monitoring_pr`, but the operation
row remains `pending` or `running` with no executor left to drive it.

Scope is limited to the active-execution PR-monitor attachment path in
`src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test that fails before the fix for non-running preserved
  active PR-monitor attachment with a stale active validate/push operation.
- Cancel superseded active validate/push operation rows before transitioning a
  non-running preserved workspace to `monitoring_pr`.
- Preserve running-workspace monitor attachment behavior without cancelling the
  active execution operation.
- Record cancellation evidence in the salvage payload for auditability.
- Keep the change scoped to worker recovery logic, tests, and plan/validation
  docs.

## Implementation Steps

1. Add focused test coverage for `validating` and `pushing` PR-monitor
   attachment with active validate/push operation rows.
2. Confirm the new test fails on the current code because the active operation
   remains active or cancellation payload evidence is absent.
3. Update `_attach_preserved_active_pr_monitor` to cancel superseded active
   execution operations when `candidate.status != WorkspaceStatus.running`.
4. Reuse or minimally generalize the existing cancellation helper so validation
   salvage behavior remains unchanged.
5. Run the targeted regression test and a narrow related worker test subset.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_pr_handoff_cancels_superseded_active_operations`
  - Passes for both `validating` and `pushing`; the original operation is
    cancelled and the monitor-attach payload lists the cancelled operation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_pr_handoff or preserved_active_pushed_branch_open_pr"`
  - Existing PR handoff coverage still passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Lint passes for touched Python files.
