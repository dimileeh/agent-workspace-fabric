# Operator Hint Protected Scope Push Blocked Validation

Plan reference: `plans/operator_hint_protected_scope_push_blocked_PLAN.md`

## Requirement Status

- Add a regression test that fails when `PROTECTED_SCOPE_PUSH_BLOCKED` leaves the
  pending operator hint unchanged: Complete.
- Mark the operator hint as `needs_human` before returning a
  `PROTECTED_SCOPE_PUSH_BLOCKED` result: Complete.
- Keep the returned `_GitPushResult` unchanged so existing monitor operation
  failure handling and terminal workspace transition behavior remain intact:
  Complete.
- Run only focused local validation; broad AWF/GitHub validation remains owned by
  AWF after this agent phase: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/operator_hint_protected_scope_push_blocked_PLAN.md`
- `plans/operator_hint_protected_scope_push_blocked_VALIDATION.md`

Focused checks run:

- Failing-first regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human -q`
  failed before the implementation because the hint remained `pending`.
- Passing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human -q`
- Guard for unchanged diff-unavailable behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result -q`
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py`

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract; AWF owns broad validation and merge gating after completion.
