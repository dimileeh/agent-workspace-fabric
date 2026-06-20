# PRRT_K37OF Sync-Base Git Env Validation

Plan reference: `plans/PRRT_K37OF_SYNC_BASE_GIT_ENV_PLAN.md`

## Requirement Status

- Confirm whether `_run_sync_base` worktree git commands currently lack the
  object lookup environment sanitizer: Complete.
  - Evidence: `_run_sync_base` used `runner._deps.runner.run(...)` without
    `env=...`; the new regression failed before the production fix because the
    fake runner observed `env is None`.
- Make sync-base worktree git runner calls pass an environment with git object
  lookup override variables removed: Complete.
  - Evidence: `src/awf/runtime/pr_monitor_runner/remote_ops.py` now passes
    `git_env_without_object_lookup_overrides()` from the sync-base `_git`
    helper.
- Add or update focused regression coverage showing sync-base git calls receive
  the sanitized environment: Complete.
  - Evidence:
    `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`
    includes `test_run_sync_base_strips_git_object_lookup_env_from_worktree_git`.
    `tests/unit/runtime/test_pr_monitor_task_tag_threading.py` updates a narrow
    fake runner signature to match the command runner contract.
- Keep validation narrow and record that full AWF/GitHub validation is owned by
  AWF after agent completion: Complete.
  - Evidence: Only targeted unit selections and targeted ruff were run. Full
    AWF/GitHub validation, coverage gates, and broad suites were not run in the
    agent phase.

## Verification Evidence

- Confirmed failing regression before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_strips_git_object_lookup_env_from_worktree_git -q`
  - Failed as expected with `assert None is not None`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  - Passed: 6 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_run_sync_base_resolves_once_and_threads_to_sink -q`
  - Passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime -k run_sync_base -q`
  - Passed: 7 passed, 2534 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py`
  - Passed.

## Gaps

None.
