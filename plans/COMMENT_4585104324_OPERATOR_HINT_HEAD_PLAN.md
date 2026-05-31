# COMMENT_4585104324 Operator Hint Head Plan

## Problem Statement And Scope

The operator-hint repair cycle captures the operation start HEAD from
`_repair_operation_start_head_result()` but discards it. If a clean leftover
repair worktree is already at a different commit than `pr_head_sha`, the
protected-scope rollback path receives the wrong baseline.

Scope is limited to the operator-hint repair cycle and its focused regression
coverage.

## Requirements Checklist

- Use the captured operation start HEAD in the protected-scope repair rollback
  call.
- Keep the existing early terminal result behavior when the operation start HEAD
  cannot be captured.
- Add regression coverage showing operator-hint protected-scope repair receives
  the captured operation start HEAD rather than the PR head SHA.
- Run only focused validation for the touched behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Rename the discarded `_operation_start_head` binding to
   `operation_start_head`.
2. Pass `operation_start_head` as both `operation_start_head` and
   `source_head_sha` to `_repair_protected_scope_commits_before_push()`, matching
   the existing CI/comment repair pattern.
3. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   for the clean-leftover-worktree baseline case.
4. Run the focused operator-hints unit test file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
