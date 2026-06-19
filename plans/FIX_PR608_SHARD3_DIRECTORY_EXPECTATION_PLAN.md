# Fix PR608 Shard 3 Directory Expectation Plan

## Problem Statement and Scope

PR #608 fails GitHub Actions check `python-coverage-shards (3)` because
`test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink`
still expects a directory at the post-validation conformance report path to
remain after cleanup. Later PR #608 cleanup work intentionally changed
`_remove_report_worktree_path` to remove directory residue at the report path
and prune empty parents.

Scope is limited to the stale regression-test expectation and its explanatory
comments. No protected workflow or quality-gate configuration files will be
changed.

## Requirements Checklist

- Preserve the existing assertion that the in-memory satisfied conformance
  artifact path is used when the synthesized worktree write fails.
- Preserve assertions that the served artifact directory receives both
  `conformance.json` and `plan.md`.
- Align the stale cleanup assertion with the current directory-cleanup
  contract.
- Run focused verification only; AWF/GitHub own the broad validation suite.

## Implementation Steps

1. Reproduce the shard failure with the single failing test.
2. Update the stale test comment and assertion to expect the report-path
   directory to be removed.
3. Re-run the focused failing test.
4. Run the nearby focused cleanup/deposit tests touched by this behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py::test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink -q`
  - Passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_removes_empty_directory tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_removes_empty_parent_directories tests/unit/control/test_executor_parts/test_executor_part_003.py::test_remove_report_worktree_path_preserves_non_empty_parent_directory -q`
  - Passes.

Full AWF/GitHub validation is intentionally not run locally under the workspace
contract.
