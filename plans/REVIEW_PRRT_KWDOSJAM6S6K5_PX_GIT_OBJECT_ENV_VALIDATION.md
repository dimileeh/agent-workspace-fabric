# Review PRRT_kwDOSJAM6s6K5-pX Git Object Env Validation

Plan reference: `plans/REVIEW_PRRT_KWDOSJAM6S6K5_PX_GIT_OBJECT_ENV_PLAN.md`

## Requirement Status

- Complete: Strip Git object lookup override environment from dirty-worktree
  status reads in `_commit_dirty_worktree`.
- Complete: Strip the same environment from dirty-worktree staging, cached-diff,
  and commit commands.
- Complete: Strip the same environment from the pre-commit autofix retry status,
  add, and commit commands.
- Complete: Add focused regression coverage proving inherited object environment
  keys are not passed to the dirty-worktree write and retry path.
- Complete: Run focused local validation only. Full AWF/GitHub validation is
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k "commit_dirty_worktree_strips_git_object_env_from_write_path"`
  - Passed: `1 passed, 17 deselected`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q -k "commit_dirty_worktree_restages_precommit_autofix_and_retries_commit"`
  - Passed: `1 passed, 16 deselected`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  - Passed: `All checks passed!`

## Gaps

None.
