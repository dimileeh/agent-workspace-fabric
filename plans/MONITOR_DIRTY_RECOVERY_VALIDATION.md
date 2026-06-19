# Monitor Dirty Recovery Validation

## Result

Implemented the highest-impact recovery gap from
`plans/MONITOR_DIRTY_RECOVERY_PLAN.md`.

## Root-Cause Coverage

- `ws_80d3fed8f7ca4b7fb58573a8` / `ws_d6a832c393d0481ba1b8e065`:
  CI repair provider retry no longer raises before the existing dirty-worktree
  commit sink runs. Safe operation-owned dirty output is committed first, so a
  later remonitor does not inherit uncommitted repair artifacts.
- `ws_8b7b1832a5fb434c818db526`:
  pre-push validation now gives an active monitor repair one bounded
  finalization pass for residual dirty state before the strict clean-worktree
  validation guard fails the push.
- Existing fail-closed behavior remains intact for direct/pre-existing dirty
  validation attempts and unrelated dirty repair worktrees.

## Verification

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py::test_ci_fix_provider_retry_commits_dirty_output_before_retry tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_validated_push_finalizes_monitor_dirty_state_before_validation -q`
  - Passed: `2 passed`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py -q`
  - Passed: `3 passed`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Passed: `26 passed`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q`
  - Passed: `10 passed`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_provider_recovery_suppression_blocks_all_monitor_agent_invocations tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py -q`
  - Passed: `23 passed`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_execute_comment_repair_pre_existing_dirty_worktree_is_terminal -q`
  - Passed: `1 passed`
- `uv run --python 3.12 --extra dev python -m ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - Passed
- `uv run --python 3.12 --extra dev python -m ruff format --check src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - Passed
- `uv run --python 3.12 --extra dev python -m mypy src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Passed

## Notes

- `uv run ... pytest` could not be used directly because the local `.venv`
  pytest wrapper still had a shebang pointing at the old repository path after
  the repo rename. `uv run ... python -m pytest` was used instead.
