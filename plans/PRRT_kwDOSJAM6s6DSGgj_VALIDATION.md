# PRRT_kwDOSJAM6s6DSGgj Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DSGgj_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `_git_show_text` and
  `_git_diff_text` include `safe.directory` for the target worktree.
- Complete: Reused the existing shared `git_safe_directory_config_args` helper
  instead of duplicating argument construction.
- Complete: Kept the implementation scoped to the PR monitor git helper
  commands and their unit coverage.
- Complete: Ran the focused regression test, the full touched unit file, ruff,
  and mypy.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/PRRT_kwDOSJAM6s6DSGgj_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DSGgj_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k git_helpers_mark_worktree_safe`
  - Failed before implementation on the missing `safe.directory` args.
  - Passed after implementation: `1 passed, 115 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q`
  - Passed: `116 passed`.

## Gaps

None.
