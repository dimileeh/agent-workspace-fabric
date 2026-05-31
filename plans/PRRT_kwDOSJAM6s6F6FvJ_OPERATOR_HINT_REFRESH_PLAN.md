# PRRT_kwDOSJAM6s6F6FvJ Operator Hint Refresh Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6FvJ` reports that merge-time operator hint refresh ignores a database hint that has the same `operation_id` as the in-memory pending hint after another monitor pass has moved that persisted hint to a terminal status (`needs_human` or `agent_failed`). The stale in-memory state can keep selecting `AddressOperatorHint` instead of `NotifyHuman`.

Scope is limited to PR monitor operator hint refresh behavior and regression coverage. No protected workflow, broad validation, branch switching, pushing, or CI-equivalent commands are in scope.

## Requirements Checklist

- Add a regression test that fails when `_refresh_operator_state_from_workspace` does not import a terminal persisted operator hint with the same `operation_id`.
- Preserve existing behavior for non-terminal same-operation hints so refresh does not churn equivalent pending state.
- Ensure terminal persisted hints cause monitor decision logic to block with `NotifyHuman` instead of dispatching `AddressOperatorHint`.
- Keep validation focused to the touched runtime test file or narrower.

## Implementation Steps

1. Add a unit regression in `tests/unit/runtime/test_pr_monitor_operator_hints.py` that loads stale pending state, updates the workspace row to a terminal same-operation hint, calls `_refresh_operator_state_from_workspace`, and asserts `decide()` returns `NotifyHuman`.
2. Update `src/awf/runtime/pr_monitor_runner/lifecycle.py` so refresh imports same-operation terminal database hints when the current in-memory hint is non-terminal or stale.
3. Run the targeted regression test first to confirm the test exposes the bug when practical, then run the narrow operator-hint unit tests needed to prove the fix.
4. Record evidence in `plans/PRRT_kwDOSJAM6s6F6FvJ_OPERATOR_HINT_REFRESH_VALIDATION.md`.
