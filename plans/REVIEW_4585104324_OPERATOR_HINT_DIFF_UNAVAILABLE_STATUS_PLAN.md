# Review 4585104324 Operator Hint Diff-Unavailable Status Plan

## Problem Statement And Scope

Review feedback reports that operator-hint repair can terminate on a protected
scope path while leaving the persisted operator hint `status="pending"`, causing
later remonitor attempts to retry the same terminal hint. The current
`PROTECTED_SCOPE_PUSH_BLOCKED` push-result path already marks the hint
`needs_human`, but the direct `ProtectedScopeDiffError` path returns a terminal
diff-unavailable push result before updating the hint status.

Scope is limited to the operator-hint repair path and focused regression
coverage for the diff-unavailable terminal result.

## Requirements Checklist

- Preserve the existing terminal `_GitPushResult` returned for
  `ProtectedScopeDiffError`.
- Mark the pending operator hint as `needs_human` before returning the terminal
  diff-unavailable result.
- Use the protected-scope refusal message as the operator hint status reason
  when available.
- Preserve the existing `PROTECTED_SCOPE_PUSH_BLOCKED` needs-human behavior.
- Run only focused local validation; AWF/GitHub own broad validation after the
  agent phase.

## Implementation Steps

1. Update the existing `ProtectedScopeDiffError` regression test to expect a
   `needs_human` operator-hint status and confirm it fails against current code.
2. Update `src/awf/runtime/pr_monitor_runner/operator_hints.py` to mark the
   hint `needs_human` after building the diff-unavailable push result and before
   returning it.
3. Re-run the focused diff-unavailable and protected-scope push-blocked tests.
4. Run targeted lint for the touched production and test files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py`

Pass criteria: both focused tests pass, targeted lint passes, and no broad
AWF/GitHub-owned validation is run in the agent phase.
