# PRRT_kwDOSJAM6s6K0ZNy Validation

## Plan Check

- Added regression assertions for all reviewed runner-based `cat-file` checks:
  fix-cycle per-item HEAD validation, stale operation-start validation, and
  mirror filesystem recovery start-head validation.
- Reused the same object-lookup override sanitization used by
  `verify_head_object_exists` by exposing it from `git_manager`.
- Kept the behavior scoped to the affected git object existence checks.

## Evidence

- Failed-first targeted tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py::test_fix_cycle_falls_back_when_per_item_head_object_is_poisoned tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_falls_back_from_stale_start_head tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch -q`
  failed with `_RecordedCall` missing `env`, confirming the new assertions
  caught the unsanitized runner path.
- Passing targeted tests:
  the same command passed with `3 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/commands.py src/awf/node/git_manager.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/common/commands.py src/awf/node/git_manager.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/remote_repair.py`
  passed.

## Broad Validation

Full AWF/GitHub validation is intentionally not run during this agent phase;
AWF owns the broad validation suite, provenance, and merge gating after agent
completion.
