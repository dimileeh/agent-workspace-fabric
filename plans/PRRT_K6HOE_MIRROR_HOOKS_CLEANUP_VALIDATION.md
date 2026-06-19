# PRRT_K6HOE Mirror Hooks Cleanup Validation

Plan reference: `plans/PRRT_K6HOE_MIRROR_HOOKS_CLEANUP_PLAN.md`

## Requirement Status

- Add a regression test showing that a non-`AgentRunError` from `adapter.run`
  triggers a second mirror hooks repair before the original exception is
  propagated: Complete.
- Keep the existing pre-launch mirror repair behavior unchanged, including
  blocking agent launch when pre-launch repair fails: Complete.
- Do not broaden exception handling into verdict parsing or dirty-worktree commit
  behavior: Complete.
- Run only focused validation for the touched test: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/comments.py`
- `tests/unit/runtime/test_pr_monitor_task_tag_threading.py`
- `plans/PRRT_K6HOE_MIRROR_HOOKS_CLEANUP_PLAN.md`
- `plans/PRRT_K6HOE_MIRROR_HOOKS_CLEANUP_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q -k "repairs_mirror_hooks_after_cleanup_failure"` failed before the implementation because the second repair was missing.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q -k "invoke_cli_for_verdict_result"` passed after the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/comments.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py` passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
