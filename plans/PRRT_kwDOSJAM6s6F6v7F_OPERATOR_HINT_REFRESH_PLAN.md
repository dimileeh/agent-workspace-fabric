# PRRT_kwDOSJAM6s6F6v7F Operator Hint Refresh Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6F6v7F` reports that
`_refresh_operator_state_from_workspace` imports pending operator hints from the
database but does not clear an in-memory pending hint when another monitor pass
has already processed that same hint and persisted only the processed marker.
This can make a merge-time refresh select `AddressOperatorHint` for work that
has already been handled.

Scope is limited to PR monitor operator-hint refresh behavior and its focused
unit coverage.

## Requirements Checklist

- Add a regression test showing a stale in-memory pending operator hint is
  cleared when the workspace database contains the matching processed marker and
  no pending hint payload.
- Ensure the refresh records or preserves the processed marker in runtime state
  so later decisions and persistence do not reselect the same hint.
- Preserve existing behavior that imports still-pending and terminal operator
  hint updates from the database.
- Avoid broad AWF/GitHub validation; run only focused tests for the changed
  behavior.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
2. Confirm the new test fails against the current implementation.
3. Update `src/awf/runtime/pr_monitor_runner/lifecycle.py` so refresh detects a
   matching processed marker for the current in-memory hint, clears the pending
   hint, and imports the marker into runtime state.
4. Run the focused unit test.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F6v7F_OPERATOR_HINT_REFRESH_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k refresh_operator_state_clears_processed_operator_hint_marker`
  - Passes after implementation.
  - Fails before implementation with the stale pending hint still present.

Full AWF/GitHub validation is intentionally not run inside this agent phase;
AWF owns broad validation, provenance, logs, and merge gating after completion.
