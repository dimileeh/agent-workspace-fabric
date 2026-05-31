# Validation Side-Effect Dirty Worktrees Validation

Plan reference: `plans/VALIDATION_SIDE_EFFECT_DIRTY_WORKTREES_PLAN.md`

## Requirement Status

- Shared validation worktree guard: Complete.
- Pre-validation dirty worktree failure: Complete.
- Post-validation side-effect cleanup: Complete.
- PR-monitor pre-push validation integration: Complete.
- PR-monitor validation-fix failed-commit rollback: Complete.
- Executor validation integration: Complete.
- Preserve strict repair-start dirty guard: Complete; the existing
  `PRE_EXISTING_DIRTY_WORKTREE` repair guard was not weakened.

## Evidence

- Added `src/awf/runtime/validation_worktree.py` for validation-owned dirty
  detection and cleanup.
- Updated PR-monitor pre-push validation to fail before validation on
  pre-existing dirt and clean validation side effects back to the captured
  pre-validation head before push or fix pass.
- Updated PR-monitor validation-fix passes to capture the fix-pass start head
  and roll back uncommitted local fix-pass edits when the agent cleanup,
  exception, or dirty-worktree commit path fails or raises.
- Updated executor validation to clean validation side effects before PR
  creation and fail immediately if cleanup cannot restore a clean worktree.
- Added regression coverage in:
  - `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - `tests/unit/control/test_executor_validation_fix_cycle.py`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Result: `25 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_003.py::test_worker_entrypoint_wires_control_worker_dependencies -q`
  - Result: `59 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
  - Result: `7 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/control/executor/execution_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_003.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/control/executor/execution_validation.py`
  - Result: passed
- `git diff --check`
  - Result: passed

## Notes

- Full repository coverage remains for GitHub/AWF CI.
- Current failed awf-cloud monitor worktrees still need operational recovery
  after this AWF fix is deployed or the monitors are recreated.
