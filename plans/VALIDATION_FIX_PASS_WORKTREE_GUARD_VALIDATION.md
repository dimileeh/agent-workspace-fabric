# Validation Fix-Pass Worktree Guard Validation

Plan reference: `VALIDATION_FIX_PASS_WORKTREE_GUARD_PLAN.md`

## Requirement Status

- Focused regression test: Complete. Added
  `test_execution_validation_rejects_fix_pass_dirty_worktree_without_reclosing_run`
  to cover a validation failure followed by a fix pass that leaves the worktree dirty.
- Avoid double-finishing the validation run: Complete. The post-fix dirty worktree
  guard now calls `_fail_validation_worktree_guard` with `validation_run_id=None`.
- Preserve pre-validation guard behavior: Complete. Nearby pre-validation dirty and
  ignored-snapshot drift tests still pass.
- Focused validation only: Complete. Full AWF/GitHub validation was not run in the
  agent phase; AWF owns broad validation, provenance, and merge gating after completion.

## Evidence

Changed files:

- `src/awf/control/executor/execution_validation.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`

Focused checks:

- Failed before the implementation change as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "fix_pass_dirty_worktree"`
- Passed after the implementation change:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "fix_pass_dirty_worktree"`
- Passed nearby guard coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "fix_pass_dirty_worktree or fix_pass_ignored_artifacts or ignored_signature_drift or new_ignored_paths_after_initial_validation_pass or dirty_before_starting_run"`
- Passed narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
- Passed whitespace check:
  `git diff --check -- src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`

## Gaps

None.
