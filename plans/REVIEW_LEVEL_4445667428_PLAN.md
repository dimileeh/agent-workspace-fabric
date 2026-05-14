# Review Level 4445667428 Plan

## Problem Statement And Scope

Address the current review-level comment `issue:4445667428`. The evidence
raises two repository-level concerns:

- `_reserve_workspace_event_orders()` calls `set_committed_value()` for
  `Workspace.version` even when `bump_version=False`, making a no-op committed
  attribute refresh for ordinary event-order reservation paths.
- `_finish_transition_if_current()` was reported as lacking a `payload`
  parameter, which would make guarded transitions unsuitable for causality
  snapshots. Current code inspection shows `transition_if_current()` already
  accepts `payload` and passes it through `_finish_transition_if_current()` into
  the `workspace.state_changed` event.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Preserve event-order reservation behavior while only refreshing
  `Workspace.version` as a committed ORM value when `bump_version=True`.
- Keep the existing `transition_if_current()` payload plumbing intact.
- Add or update a focused regression test for the non-version-bumping event
  reservation path.
- Run the narrow database repository test that covers the change.
- Run ruff on the touched Python files.
- Commit the local fix with a conventional commit message referencing the
  review comment id.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Update `tests/unit/db/test_workspace_repository.py` so the batch/add-event
   reservation test records committed-value refreshes and fails if the helper
   refreshes `version` when `bump_version=False`.
2. Run that focused test and confirm the existing implementation fails the new
   assertion when practical.
3. Change `_reserve_workspace_event_orders()` to call
   `set_committed_value(workspace, "version", new_version)` only under
   `bump_version=True`.
4. Re-run the focused test and ruff.
5. Write `plans/REVIEW_LEVEL_4445667428_VALIDATION.md` with evidence and any
   remaining gaps.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_reserves_event_order_without_advancing_workspace_version -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py`
  must pass.
