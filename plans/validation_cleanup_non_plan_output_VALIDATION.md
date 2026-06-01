# Validation Cleanup Non-Plan Output Validation

Plan reference: `validation_cleanup_non_plan_output_PLAN.md`

## Requirement Status

- Complete: Dirty validation-worktree guard preserves
  `has_known_non_plan_output=True`.
- Complete: Cleanup guard failure preserves `has_known_non_plan_output=True`.
- Complete: Existing validation run finalization, pending operation failure, and
  workspace failure assertions remain covered by the updated focused tests.
- Complete: Only focused local checks were run; broad AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/validation_cleanup_guards.py`
- `src/awf/control/executor/execution_validation.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`

Commands run:

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_fails_when_worktree_is_dirty_before_starting_run tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  - Result: failed with `result.has_known_non_plan_output` returned as `False`.
- Passing regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_fails_when_worktree_is_dirty_before_starting_run tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  - Result: `3 passed in 0.98s`.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/validation_cleanup_guards.py src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`
  - Result: `All checks passed!`.

No remaining gaps.
