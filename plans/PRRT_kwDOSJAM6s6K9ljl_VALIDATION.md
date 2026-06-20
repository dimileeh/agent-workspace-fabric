# PRRT_kwDOSJAM6s6K9ljl Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K9ljl_PLAN.md`

## Requirement Status

- Complete: A non-`AgentRunError` after the agent starts still repairs the
  mirror hooks path when a mirror exists.
- Complete: The wrapper invokes `_commit_dirty_worktree` after post-failure
  mirror repair, forwarding the commit message, compose context, state, task tag,
  and `operation_start_head`.
- Complete: The original runtime/plumbing exception is still rethrown after the
  sink completes. Existing sink safety exceptions remain fail-closed.
- Complete: Focused regression coverage demonstrates the new call order. Broad
  AWF/GitHub validation is intentionally not run in the agent phase.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/comments.py` to call
  `_commit_dirty_worktree` in the generic adapter/runtime exception path after
  mirror hook repair and before rethrowing.
- Updated
  `tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_repairs_mirror_hooks_after_cleanup_failure`
  to require the post-failure sink call and verify operation context forwarding.

## Commands

- Initial expected failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_repairs_mirror_hooks_after_cleanup_failure -q`
  failed before implementation because `_commit_dirty_worktree` was not called.
- Passing focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_repairs_mirror_hooks_after_cleanup_failure -q`
- Passing neighboring wrapper checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_repairs_mirror_hooks_before_agent tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_repairs_mirror_hooks_after_cleanup_failure tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_invoke_cli_for_verdict_result_blocks_agent_when_mirror_hook_repair_fails -q`
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/comments.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py`

## Gaps

None. Full AWF/GitHub validation, coverage gates, and merge checks are managed by
AWF after agent completion.
