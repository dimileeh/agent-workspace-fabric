# PRRT_kwDOSJAM6s6K4aW- Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K4aW-_PLAN.md`

## Requirement Status

- Complete: Repair mirror `core.hooksPath` before launching the CI fix adapter.
  - Evidence: `src/awf/runtime/pr_monitor_runner/ci_ops.py` now calls
    `mirror_path_for_worktree()` and `repair_mirror_hooks_path()` after runtime
    ownership repair and before `adapter.run()`.
- Complete: Fail closed without launching the adapter when mirror hook repair
  fails.
  - Evidence:
    `test_run_ci_fix_blocks_agent_when_mirror_hook_repair_fails` asserts the
    adapter and dirty-worktree sink are not called and the result carries the
    `MIRROR_HOOKS_PATH_POISONED` reason code.
- Complete: Preserve existing post-agent `_commit_dirty_worktree()` behavior.
  - Evidence: The existing call remains after `adapter.run()`, and
    `test_run_ci_fix_repairs_mirror_hooks_before_agent` asserts the order is
    mirror repair, adapter run, dirty-worktree commit.
- Complete: Add focused regression tests for ordering and failure behavior.
  - Evidence: Two new tests in
    `tests/unit/runtime/test_pr_monitor_task_tag_threading.py`.
- Complete: Run only targeted validation.
  - Evidence: Commands below were focused on the changed runtime path and test
    file. Full AWF/GitHub validation remains managed by AWF after agent
    completion.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q -k "run_ci_fix"`
  - Passed: `3 passed, 21 deselected`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py`
  - Passed: `All checks passed!`

## Remaining Gaps

None.
