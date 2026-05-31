# Validation Side-Effect Dirty Worktrees Validation

Plan reference: `plans/VALIDATION_SIDE_EFFECT_DIRTY_WORKTREES_PLAN.md`

## Requirement Status

- Shared validation worktree guard: Complete.
- Pre-validation dirty worktree failure: Complete.
- Post-validation side-effect cleanup: Complete.
- PR-monitor pre-push validation integration: Complete.
- Executor validation integration: Complete.
- Preserve strict repair-start dirty guard: Complete; the existing
  `PRE_EXISTING_DIRTY_WORKTREE` repair guard was not weakened.

## Evidence

- Added `src/awf/runtime/validation_worktree.py` for validation-owned dirty
  detection and cleanup.
- Updated PR-monitor pre-push validation to fail before validation on
  pre-existing dirt and clean validation side effects back to the captured
  pre-validation head before push or fix pass.
- Updated executor validation to clean validation side effects before PR
  creation and fail immediately if cleanup cannot restore a clean worktree.
- Added regression coverage in:
  - `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - `tests/unit/control/test_executor_validation_fix_cycle.py`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/control/test_executor_validation_fix_cycle.py -q`
  - Result: `53 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
  - Result: `7 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/control/executor/execution_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/control/test_executor_validation_fix_cycle.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/control/executor/execution_validation.py`
  - Result: passed
- `git diff --check`
  - Result: passed

## Notes

- Full repository coverage remains for GitHub/AWF CI.
- Current failed awf-cloud monitor worktrees still need operational recovery
  after this AWF fix is deployed or the monitors are recreated.
