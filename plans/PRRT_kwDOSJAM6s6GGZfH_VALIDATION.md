# PRRT_kwDOSJAM6s6GGZfH Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GGZfH_PLAN.md`

## Requirement Status

- Complete: `check_validation_worktree_clean` runs before missing workspace
  `HEAD` is classified as `VALIDATION_INFRASTRUCTURE_ERROR`.
- Complete: dirty worktree and missing-HEAD guard failures still start and
  finalize validation runs with reason-code provenance.
- Complete: regression coverage proves `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`
  wins when the worktree is dirty and `HEAD` capture fails.
- Complete: validation remained focused. Full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

- Changed `src/awf/control/executor/execution_validation.py`.
- Changed
  `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`.
- Reproducer before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_reports_dirty_worktree_when_head_capture_fails -q`
  failed because `check_validation_worktree_clean` was not awaited.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_reports_dirty_worktree_when_head_capture_fails -q`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
  passed.
