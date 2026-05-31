# Review PRRT_kwDOSJAM6s6F56Tj Processed Operator Hint Plan

## Problem Statement And Scope

An inline review reports that a stale monitor loop can resurrect a pending
operator hint after another loop has already processed it. The fix is scoped to
PR monitor state persistence for operator hints and its focused regression
coverage.

## Requirements Checklist

- Reproduce the stale persist path where the database has a processed operator
  hint marker and no pending hint, while runtime state still has the same
  pending hint.
- Preserve the processed marker from the database when persisting stale runtime
  state.
- Do not re-persist `OPERATOR_HINT_STATE_KEY` for a hint that has already been
  marked processed.
- Keep unrelated monitor state updates from the stale loop, such as newly
  addressed review threads.
- Avoid broad AWF/GitHub-owned validation; run focused checks only.

## Implementation Steps

1. Add a unit regression test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
2. Confirm the new test fails against the current implementation.
3. Update the operator hint merge path in
   `src/awf/runtime/pr_monitor_runner/lifecycle.py` to preserve database
   processed markers and clear matching stale pending hints.
4. Re-run the focused regression test and a nearby existing operator hint test.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k 'processed_marker or round_trips_pending_operator_hint'`

Pass criteria: the focused tests pass, and full AWF/GitHub validation remains
owned by AWF after agent completion.
