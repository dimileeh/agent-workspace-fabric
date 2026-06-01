# Pre-Push Validation Cleanup Side Effects Plan

## Problem Statement and Scope

PR monitor pre-push validation currently allows a push when validation passes, cleanup successfully removes validation-written worktree side effects, and the restored commit state was never validated. This can record a passing pre-push validation for code that is not actually pushed.

Scope is limited to the PR monitor pre-push validation path and focused regression coverage for the review comment 4404091773. Broad AWF/GitHub validation remains owned by AWF after agent completion.

## Requirements Checklist

- Reject a passing pre-push validation when cleanup reports restored or deleted side effects.
- Preserve cleanup evidence, including cleaned paths, in the returned push failure details.
- Mark the validation run failed with the side-effect cleanup reason code.
- Prevent `git push` from running in the side-effect cleanup case.
- Keep existing cleanup-failure behavior unchanged.

## Implementation Steps

1. Convert the existing pre-push side-effect test into a stricter regression that expects failure instead of push.
2. Confirm the focused regression fails before implementation.
3. Add pre-push handling that converts successful validation plus cleaned side effects into a synthetic validation failure.
4. Keep the synthetic side-effect failure terminal for pre-push fix-pass selection.
5. Run the focused regression test and any adjacent targeted test needed for cleanup behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_tracked_side_effect_after_validation_blocks_push -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py::test_pre_push_validation_cleanup_failure_blocks_push -q`
  - Passes to confirm cleanup-failure blocking remains intact.
