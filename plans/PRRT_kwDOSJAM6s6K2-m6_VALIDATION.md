# PRRT_kwDOSJAM6s6K2-m6 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K2-m6_PLAN.md`

## Requirement Status

- Complete: Detect untracked paths in the staged recovery candidate before policy refresh.
  - Added `_untracked_cleanup_paths_from_name_status_z` for staged `--name-status -z`
    records that can leave untracked files after `reset --hard`.
- Complete: On policy-blocked recovery, reset tracked/index state and remove those
  untracked recovery paths with a literal pathspec clean.
  - The policy-blocked branch now runs `git clean -fd -- <paths>` after a successful
    hard reset when cleanup paths exist.
- Complete: Preserve policy-blocked error propagation and existing cleanup logging behavior.
  - `_MonitorPolicyBlockedError` is still raised with the original policy message;
    reset cleanup warning is unchanged and clean failure has a dedicated warning.
- Complete: Do not clean runtime-excluded paths that were unstaged before policy refresh.
  - Cleanup paths are recomputed after runtime paths are unstaged.
- Complete: Add a focused regression test for policy-blocked cleanup of untracked recovery files.
  - Added `test_recover_missing_head_object_policy_block_cleans_recovery_untracked_paths`
    plus direct parser tests for cleanup-path extraction and malformed output.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `plans/PRRT_kwDOSJAM6s6K2-m6_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K2-m6_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_policy_block_cleans_recovery_untracked_paths -q`
  - Failed before implementation because no `git clean` call was issued.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_policy_block_cleans_recovery_untracked_paths -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`
  - Passed: 25 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, and merge gating after agent completion.
