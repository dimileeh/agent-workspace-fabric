# PRRT_kwDOSJAM6s6F54j3 Operator Hint Merge Recheck Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F54j3` reports that the PR monitor can decide
`Merge` from a stale in-memory `MonitorState` even after an operator remonitor
hint has been persisted to `Workspace.monitor_threads_addressed`. The current
persist path preserves concurrent hints, but the merge path can still reach
`merge_pr` before a later persist/reload observes the DB hint.

Scope is limited to the PR monitor merge path, operator-hint state refresh, a
focused regression, and this plan/validation record.

## Requirements Checklist

- Recheck persisted operator hint state immediately before attempting an
  auto-merge.
- If a newly persisted pending operator hint is found, route through the
  existing `AddressOperatorHint` action instead of calling `merge_pr`.
- Preserve in-memory monitor state such as reviewer-settle and feedback markers
  while importing only the concurrent DB operator hint needed to block merge.
- Add a regression test for a stale `Merge` action with a concurrently persisted
  DB operator hint.
- Run focused tests only; broad AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add a failing runner regression for stale merge-state bypass of a persisted
   operator hint.
2. Add a small lifecycle helper that refreshes `MonitorState.pending_operator_hint`
   from the current workspace row without discarding the existing in-memory
   state.
3. Call the helper inside the merge critical section before the final queue/gate
   and merge operation, and re-run `decide()` against the refreshed state.
4. If the refreshed decision is not `Merge`, dispatch it through the existing
   `_execute()` recursion path.
5. Run the focused operator-hint regression test file or targeted test subset.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "operator_hint"`
  passes.
- The validation document records the targeted evidence and notes that broad
  AWF/GitHub validation is intentionally left to AWF.
