# PRRT_kwDOSJAM6s6K9T6A Sync-Base HEAD Guard Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K9T6A_SYNC_BASE_HEAD_GUARD_PLAN.md`

## Requirement Status

- Preserve fail-closed behavior when post-agent mirror hook repair fails:
  Complete. Existing focused test coverage remains in
  `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`.
- Invoke the dirty-worktree commit sink after successful post-agent mirror hook
  repair for a non-`AgentRunError`: Complete. The cleanup-failure regression now
  expects `_commit_dirty_worktree` to run after hook repair.
- Propagate sink failures according to existing sync-base handling: Complete.
  The implementation reuses the existing `_commit_dirty_worktree` exception
  handlers before re-raising the original cleanup/runtime error.
- Preserve the original adapter/runtime exception when the sink succeeds:
  Complete. The regression asserts the original `ComposeExecCleanupError`
  instance is re-raised after the sink runs.
- Keep validation focused: Complete. Only targeted unit tests and a narrow Ruff
  check were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`
- `plans/PRRT_kwDOSJAM6s6K9T6A_SYNC_BASE_HEAD_GUARD_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K9T6A_SYNC_BASE_HEAD_GUARD_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_repairs_mirror_hooks_after_conflict_agent_cleanup_failure -q`
  failed because the cleanup path did not call `_commit_dirty_worktree`.
- Green focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_repairs_mirror_hooks_after_conflict_agent_cleanup_failure -q`
  passed.
- Focused sync-base file:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  passed with 11 tests.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`
  passed.

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns
the broad validation suite, provenance, and merge gate after completion.
