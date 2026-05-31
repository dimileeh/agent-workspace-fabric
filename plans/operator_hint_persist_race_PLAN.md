# Operator Hint Persist Race Plan

## Problem Statement

An in-flight PR monitor can load `MonitorState` before an operator remonitor
request persists a pending operator hint and reviewer-settle freeze markers.
The old monitor loop later calls `_persist_state()` with stale in-memory state
and can overwrite `Workspace.monitor_threads_addressed`, dropping the hint and
re-arming elapsed settle markers. That can let auto-merge proceed without
processing the operator warning.

## Scope

- Fix `src/awf/runtime/pr_monitor_runner/lifecycle.py` state persistence.
- Add focused regression coverage in the PR monitor operator-hint tests.
- Do not run broad AWF/GitHub-owned validation; record focused local checks only.

## Requirements Checklist

- Preserve a concurrently persisted DB operator hint when the in-memory
  `MonitorState` did not know about it.
- Preserve past-settle freeze markers written by remonitor so stale elapsed
  initial-review/non-check reviewer markers are not restored.
- Do not resurrect a hint that the current state has already processed.
- Keep existing monitor addressed-thread and sync-base state persistence
  behavior intact.

## Implementation Steps

1. Add a regression test that seeds stale elapsed settle state, loads a stale
   `MonitorState`, simulates `remonitor_workspace()` writing a pending hint and
   freeze markers in the DB, then calls `_persist_state()` with the stale state.
2. Confirm the regression fails before the fix.
3. Update `_persist_state()` to merge current DB operator hint and freeze
   markers into the persisted map unless the current state is processing or has
   processed that same hint.
4. Run focused tests for operator hints and the nearby sync-base persistence
   regression.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::test_sync_base_no_progress_state_is_persisted_across_restarts -q`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
