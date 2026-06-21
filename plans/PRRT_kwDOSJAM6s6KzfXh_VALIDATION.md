# PRRT_kwDOSJAM6s6KzfXh Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6KzfXh_PLAN.md`

## Requirement Status

- Add a focused regression test for recovered committed protected-scope changes:
  Complete. Added
  `test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit`.
- Validate recovered commit ranges as committed diffs against the recovery base:
  Complete. `_commit_dirty_worktree` now checks `recovery_head..recovered`
  protected files through committed diff loading.
- Block recovered commits that contain unowned protected-scope changes, including
  when compose repair context is unavailable: Complete. The recovered committed
  violation raises `_MonitorPolicyBlockedError` before returning `True`.
- Preserve existing behavior for runtime-only recovered diffs and normal dirty
  worktree commits: Complete. Existing focused runner tests passed.
- Run focused tests only: Complete. Full AWF/GitHub validation was not run in
  the agent phase and remains managed by AWF after completion.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
- `plans/PRRT_kwDOSJAM6s6KzfXh_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6KzfXh_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit -q`
  - Failed before implementation because `_MonitorPolicyBlockedError` was not raised.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  - Passed: 24 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  - Passed after import ordering fix.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py`
  - Passed.

## Gaps

No planned gaps remain. Broad validation and coverage gates are intentionally
left to AWF/GitHub after the agent phase per the workspace contract.
