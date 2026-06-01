# Validation Side Effects Validation

Plan reference: `plans/VALIDATION_SIDE_EFFECTS_PLAN.md`

## Requirement Status

- Reject a successful validation when post-validation cleanup had to restore or delete side effects: Complete.
- Keep true no-op cleanup success valid: Complete. The new guard only triggers when cleanup reports side-effect paths or a dirty cleanup check.
- Preserve cleanup failure guard behavior: Complete. Existing `not cleanup_result.ok` handling is unchanged and remains covered by the focused executor cleanup file.
- Record a clear validation failure reason and artifact evidence: Complete. The executor now records `VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED` with a synthetic failure artifact listing cleaned paths.
- Add a focused regression test: Complete. Added `test_execution_validation_rejects_success_after_cleaned_side_effects`.
- Run only targeted checks: Complete. Full AWF/GitHub validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/execution_validation.py`
- `src/awf/runtime/validation_worktree.py`
- `src/awf/runtime/validation_worktree_constants.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree_head_cleanup.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_rejects_success_after_cleaned_side_effects -q`
  - Initial red result: failed at import because `VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED` did not exist.
  - Final green result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py -q`
  - Result: 23 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py -q`
  - Result: 61 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py src/awf/runtime/validation_worktree.py src/awf/runtime/validation_worktree_constants.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py`
  - Result: passed after import-order fix.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py src/awf/runtime/validation_worktree.py src/awf/runtime/validation_worktree_constants.py`
  - Result: passed.

No remaining gaps.
