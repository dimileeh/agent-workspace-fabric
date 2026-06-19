# Fix PR608 Shard 3 Directory Expectation Validation

Plan reference: `FIX_PR608_SHARD3_DIRECTORY_EXPECTATION_PLAN.md`

## Requirement Status

- Preserve the in-memory satisfied conformance artifact assertion: Complete.
  The test still asserts `deposit_workspace_planning_artifacts` is not invoked
  for the failed synthesized write branch and still reads the served
  `conformance.json`.
- Preserve the served `conformance.json` and `plan.md` assertions: Complete.
  The test still verifies the deposited report status/summary and the copied
  plan content.
- Align the cleanup assertion with the current directory-cleanup contract:
  Complete. The stale expectation now asserts the report-path directory is
  removed after fallback cleanup.
- Run focused verification only: Complete. Broad AWF/GitHub validation was not
  run locally; AWF/GitHub own full validation after agent completion.

## Evidence

Files changed:

- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py`
- `plans/FIX_PR608_SHARD3_DIRECTORY_EXPECTATION_PLAN.md`
- `plans/FIX_PR608_SHARD3_DIRECTORY_EXPECTATION_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py::test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink -q`
  - Result: passed (`1 passed`).
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_removes_empty_directory tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_removes_empty_parent_directories tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_preserves_non_empty_parent_directory -q`
  - Result: passed (`3 passed`).

No remaining gaps.
