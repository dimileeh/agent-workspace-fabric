# PRRT_kwDOSJAM6s6F56Ti Freeze Recheck Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F56Ti` reports that a no-reason operator
remonitor after reviewer settle re-arms persisted grace/settle markers without
persisting an operator hint. The merge critical-section recheck currently only
imports pending operator hints, so an already-running monitor can keep stale
elapsed settle state in memory and still call `merge_pr`.

Scope is limited to merge-time monitor-state refresh, focused regression
coverage, and this plan/validation record.

## Requirements Checklist

- Recheck persisted no-reason remonitor freeze state before the final merge
  attempt.
- Preserve the existing pending-operator-hint merge recheck behavior.
- Preserve in-memory review feedback state while importing only concurrent
  operator remonitor wait markers needed to block stale merge.
- Re-evaluate non-check reviewer settle after importing freeze-only state and
  wait instead of merging when the re-armed settle window is active.
- Add a regression proving a stale in-memory elapsed settle state cannot merge
  after a concurrent no-reason remonitor re-arms settle markers.
- Run focused tests/lint only; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a failing regression in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   for stale merge state plus DB-only no-reason remonitor freeze markers.
2. Extend the lifecycle refresh helper so it can import concurrent operator
   freeze wait markers from the workspace row without discarding other in-memory
   monitor state.
3. Update the merge critical section to re-run the non-check reviewer settle
   gate when the refresh imports freeze-only state.
4. If the re-evaluated settle gate requires waiting, exit the merge path via
   the existing `reviewer_settle_wait` monitor-state operation instead of
   calling `merge_pr`.
5. Run the focused regression and narrow lint/type checks for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "freeze_only_remonitor"`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "operator_hint or freeze_only_remonitor"`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/mixins.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passes after implementation.
