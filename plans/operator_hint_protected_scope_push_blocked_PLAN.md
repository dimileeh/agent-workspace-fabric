# Operator Hint Protected Scope Push Blocked Plan

## Problem Statement

An operator remonitor hint repair can return a terminal
`PROTECTED_SCOPE_PUSH_BLOCKED` push result while the in-memory operator hint still has
`status="pending"`. When the monitor persists that unchanged state, a later
remonitor can reload and retry the same unpushable hint instead of surfacing it
for human decision.

## Scope

- Update only the operator-hint repair flow for the protected-scope push-blocked
  terminal result.
- Preserve existing behavior for protected-scope diff-unavailable failures and
  other terminal monitor failures unless a focused test proves otherwise.

## Requirements Checklist

- Add a regression test that fails when `PROTECTED_SCOPE_PUSH_BLOCKED` leaves the
  pending operator hint unchanged.
- Mark the operator hint as `needs_human` before returning a
  `PROTECTED_SCOPE_PUSH_BLOCKED` result.
- Keep the returned `_GitPushResult` unchanged so existing monitor operation
  failure handling and terminal workspace transition behavior remain intact.
- Run only focused local validation; broad AWF/GitHub validation remains owned by
  AWF after this agent phase.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   that stubs protected-scope repair to return `PROTECTED_SCOPE_PUSH_BLOCKED` and
   asserts the hint becomes `needs_human`.
2. Run that specific test and confirm it fails before the production change.
3. Update `src/awf/runtime/pr_monitor_runner/operator_hints.py` to mark the
   pending hint as `needs_human` when the push result has reason code
   `PROTECTED_SCOPE_PUSH_BLOCKED`.
4. Re-run the focused test and a nearby existing operator-hint protected-scope
   test to guard the unchanged diff-unavailable behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result -q`

Pass criteria: the new test fails before the implementation, then both focused
tests pass after the implementation.
