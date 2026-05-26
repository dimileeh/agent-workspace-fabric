# Fix-Pass Git Cleanup Plan

## Problem Statement And Scope

CodeRabbit review comment `4366503484` reports that validation fix-pass git failures can return `ExecutionValidationResult(stop=True, ...)` without closing pending validate operations or moving the workspace out of `validating`. The scoped fix is limited to git failures in the validation fix-pass commit block in `src/awf/control/executor/execution_validation.py`.

## Requirements Checklist

- Add regression coverage proving fix-pass git failures close recovery-created validate operations.
- Ensure `git add -A`, `git diff --cached --name-only`, and fix-pass `git commit` failures finish pending validate operations as failed.
- Ensure those terminal branches mark the workspace failed with an infrastructure failure instead of leaving it `validating`.
- Preserve existing successful fix-pass behavior and keep changes scoped to the review finding.
- Use focused validation only; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add targeted tests in `tests/unit/control/test_executor_validation_fix_cycle.py` for fix-pass add, diff, and commit command failures with a pending validate operation.
2. Run the targeted tests to confirm the new regression fails against current code.
3. Add a small local cleanup helper in `execution_validation.py` for fix-pass git command failures.
4. Call that helper from the fix-pass add, diff, and commit failure branches before returning.
5. Re-run the targeted test file or selected test class.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py -q`

Pass criteria: the focused test command passes, and the validation document records that broader AWF/GitHub validation was not run inside the agent phase per workspace contract.
