# Recovered Rename Paths Validation

Plan reference: `plans/RECOVERED_RENAME_PATHS_PLAN.md`

## Requirement Status

- Use a recovered committed diff format that includes rename source and
  destination paths: Complete.
- Preserve the existing agent-runtime path filtering and no-op behavior:
  Complete.
- Fail closed if the recovered committed diff output is malformed: Complete.
- Add focused regression coverage for rename source preservation: Complete.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests/checks:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
- `plans/RECOVERED_RENAME_PATHS_PLAN.md`
- `plans/RECOVERED_RENAME_PATHS_VALIDATION.md`

Test-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_includes_rename_sources -q`
  failed before implementation because `changed_paths` was
  `("R100", ".github/workflows/ci.yml", "docs/ci.yml")`.

Verification run after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_runtime_only_returns_false tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_includes_rename_sources -q`
  passed: 3 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  passed: 26 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
