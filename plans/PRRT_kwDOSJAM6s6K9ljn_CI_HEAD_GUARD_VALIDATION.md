# PRRT_kwDOSJAM6s6K9ljn CI HEAD Guard Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K9ljn_CI_HEAD_GUARD_PLAN.md`

## Requirement Status

- Preserve fail-closed behavior when post-agent mirror hook repair fails:
  Complete. The focused cleanup-failure regression still expects
  `_MonitorMirrorHooksPathRepairFailedError` when the second hook repair fails.
- Run `_commit_dirty_worktree` after successful post-agent mirror hook repair
  for a non-`AgentRunError`: Complete. The regression now asserts the sink runs
  after the second hook repair.
- Forward the original `operation_start_head`: Complete. The regression asserts
  the sink receives `abc123`, the operation-start HEAD captured before the CI
  agent launch.
- Preserve existing commit-sink reason-code handling: Complete. The
  implementation reuses the existing `_commit_dirty_worktree` exception handlers
  before re-raising the original runtime exception.
- Preserve the original adapter/runtime exception when the sink succeeds:
  Complete. The regression still asserts the original `ComposeExecCleanupError`
  is re-raised after the sink runs.
- Keep validation focused: Complete. Full AWF/GitHub validation is intentionally
  not run in the agent phase.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/ci_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
- `plans/PRRT_kwDOSJAM6s6K9ljn_CI_HEAD_GUARD_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K9ljn_CI_HEAD_GUARD_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_ci_fix_cleanup_error_repairs_hooks_path -q`
  failed because the cleanup path did not call `_commit_dirty_worktree`.
- Green focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_ci_fix_cleanup_error_repairs_hooks_path -q`
  passed.
- Neighboring HEAD-object handling check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_ci_fix_cleanup_error_repairs_hooks_path tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_ci_fix_catches_head_object_missing_error -q`
  passed.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  passed.

Full AWF/GitHub validation, coverage gates, and merge checks remain managed by
AWF after agent completion.
