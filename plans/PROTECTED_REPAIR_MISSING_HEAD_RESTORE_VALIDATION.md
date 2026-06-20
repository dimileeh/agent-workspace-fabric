# Protected Repair Missing HEAD Restore Validation

Plan reference: `plans/PROTECTED_REPAIR_MISSING_HEAD_RESTORE_PLAN.md`

## Requirement Status

- Capture the pre-repair HEAD before invoking the repair agent: Complete.
  `_repair_protected_scope_changes_before_commit` now snapshots
  `pre_repair_head` before adapter launch.
- Restore the worktree ref before raising `_MonitorHeadObjectMissingError`:
  Complete. Missing post-repair HEAD-object verification now attempts
  `git reset --hard <pre_repair_head>` before raising the existing error.
- Run restore Git commands without object-lookup override environment variables:
  Complete. The reset uses `git_env_without_object_lookup_overrides()`, and the
  regression asserts private object directory variables are absent.
- Preserve the existing reason code and failure classification: Complete. The
  raised `_MonitorHeadObjectMissingError` still uses
  `HEAD_OBJECT_MISSING_UNRECOVERABLE`.
- Keep validation focused: Complete. Full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_restores_pre_repair_head_before_missing_head_error -q`
  - Failed before implementation because no reset call was made.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`
  - Passed: 26 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_protected_scope_repair_returns_none_when_recheck_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py::test_protected_scope_repair_raises_on_ownership_repair_failure tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_030.py::test_protected_scope_repair_filters_remote_restored_status_violation tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_030.py::test_protected_scope_repair_filters_remote_restored_remaining_violation -q`
  - Passed: 4 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  - Passed.

No broad validation, coverage gate, frontend build, push, or branch operation was
run; AWF/GitHub owns those after agent completion.
