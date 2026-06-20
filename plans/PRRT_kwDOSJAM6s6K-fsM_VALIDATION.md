# PRRT_kwDOSJAM6s6K-fsM sync handoff mirror hook repair validation

## Plan Validation

- Inspect feature-workspace and monitor handoff setup paths: Complete. The
  feature path repaired mirror hooks before setup, after setup failure, and
  after successful setup; the monitor handoff setup path did not.
- Add a focused regression for setup/pre_agent failure repair: Complete.
  `test_sync_feature_pr_handoff_repairs_mirror_hooks_after_setup_failure`
  verifies repair before setup and again before terminal setup failure.
- Add coverage for fail-closed repair failure: Complete.
  `test_sync_feature_pr_handoff_mirror_hooks_repair_failure_blocks_setup`
  verifies setup is skipped and the workspace fails with
  `MIRROR_HOOKS_PATH_REPAIR_FAILED`.
- Implement the smallest handoff setup change: Complete. The handoff setup path
  now uses the existing mirror hook repair helper before setup, after cleanup
  failure, after successful setup, and after setup command failure.

## Focused Checks

- Initial focused regression run failed before implementation because
  `monitor_handoff_setup` exposed no `mirror_path_for_worktree` repair hook to
  patch.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_repairs_mirror_hooks_after_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_mirror_hooks_repair_failure_blocks_setup -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
  passed.

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns
the broad validation, provenance, and merge gate after completion.
