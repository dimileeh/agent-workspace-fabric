# Address Review Thread PRRT_kwDOSJAM6s6GSXT0 Plan

## Problem Statement and Scope

The planning-scope auto-retry helper records
`workspace.planning_scope_auto_retry_failed` after `retry_workspace_row` raises a
retry or host-port error. The review reports that this failure-event write should
first roll back the session so any transactional state from the retry attempt is
not committed with the event.

Scope is limited to the executor planning auto-retry failure path and a focused
unit regression. Full AWF/GitHub validation remains owned by AWF after this agent
phase.

## Requirements Checklist

- [ ] Add a regression that fails if the failure-event path commits before
  rolling back retry transaction state.
- [ ] Roll back the session before recording
  `workspace.planning_scope_auto_retry_failed` for `WorkspaceRetryError`,
  `WorkspaceCreateDuplicateHostPortError`, and
  `WorkspaceCreateHostPortConflictError`.
- [ ] Preserve existing failure-event payload contents and commit behavior.
- [ ] Run only focused checks for the touched executor behavior.
- [ ] Commit the review-thread fix locally on the current AWF branch.

## Implementation Steps

1. Add a focused executor test to record session operations and assert rollback
   occurs before event recording and the final commit.
2. Run the targeted test before the production change to confirm it fails.
3. Roll back real executor sessions in the retry exception handler before
   re-fetching the workspace and calling `repo.add_event(...)`.
4. Re-run the targeted test and a focused lint check for the changed files.
5. Write the validation artifact with evidence and commit the changed files.

## Assumptions/Changes

- The nearby existing coverage-edge test file is owned by `root:root` and not
  writable from this workspace user. The regression is therefore added as a new
  focused unit file under the same `tests/unit/control` area instead of editing
  that file.
- The production rollback call is tolerant of minimal test doubles that do not
  expose `rollback`, while real executor `AsyncSession` instances still roll
  back before the failure event is re-read and committed.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q`
  - Passes after the implementation; fails before the implementation on the new
    rollback ordering assertion.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_auto_retry_planning_scope_failure_records_skip_and_retry_errors -q`
  - Passes to preserve the existing focused failure-event payload behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_executor_planning_auto_retry_transactions.py`
  - Reports no lint issues in the touched files.
