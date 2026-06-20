# PRRT_kwDOSJAM6s6KzsaM Plan

## Problem Statement And Scope

The recovered missing-HEAD pre-push validation fix-pass path detects committed
protected-scope violations and rolls back to `fix_start_head`, but after a
successful rollback it returns `(False, None)`. The parent fix-pass loop treats
that as a generic `PRE_PUSH_VALIDATION_FIX_FAILED` instead of preserving the
protected-scope failure reason.

Scope is limited to the fix-pass return reason, focused unit coverage, and this
plan/validation documentation.

## Requirements Checklist

- Preserve `PROTECTED_SCOPE_REPAIR_FAILED` when a recovered missing-HEAD
  fix-pass commit contains protected-scope violations.
- Keep rollback behavior unchanged, including surfacing rollback failure reasons
  when rollback itself fails.
- Preserve clean recovered-commit behavior.
- Add focused regression coverage for the review thread.
- Do not run broad AWF/GitHub-owned validation in the agent phase.

## Implementation Steps

1. Update the recovered protected-scope violation regression to expect the
   protected-scope failure reason after successful rollback.
2. Return the protected-scope repair failure reason from the recovered violation
   branch when rollback succeeds.
3. Run the targeted affected regression and a focused ruff check on touched
   Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::test_pre_push_validation_fix_pass_blocks_recovered_commit_protected_scope_violations -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
  must pass.
- Full AWF/GitHub validation is managed by AWF after agent completion.
