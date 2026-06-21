# Recovery Anchor Validation Validation

Plan reference: `plans/RECOVERY_ANCHOR_VALIDATION_PLAN.md`

## Requirement Status

- Validate a non-empty `operation_start_head` against the mirror before using it
  as the filesystem recovery anchor: Complete.
- Fall back to `_open_merge_candidate_head_sha` when the captured
  `operation_start_head` is not resolvable as a commit: Complete.
- Preserve existing behavior when `operation_start_head` is resolvable or when no
  fallback head exists: Complete for the scoped caller behavior; existing focused
  dirty-worktree recovery tests remain green.
- Add focused regression coverage for the stale-anchor fallback path: Complete.
- Run only targeted validation; full AWF/GitHub validation remains owned by AWF
  after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_falls_back_from_stale_start_head -q`
  - First run before implementation failed because recovery used the stale
    operation-start SHA.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  - Passed: 25 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
