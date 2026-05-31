# PRRT_kwDOSJAM6s6F67kW Commit Autofix Rename Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F67kW_commit_autofix_rename_PLAN.md`

## Requirement Status

- Add a regression test that reproduces a worktree-modified rename reported as
  `RM old -> new`: Complete.
- Preserve the existing safety rule that unrelated worktree-modified paths are
  rejected: Complete.
- Treat only the new side of a worktree-modified rename as needing a hook repair
  path match: Complete.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F67kW_commit_autofix_rename_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F67kW_commit_autofix_rename_VALIDATION.md`

Regression evidence:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_modified_rename_destination -q`
  failed because the retry returned `None` and logged
  `worktree_modified_paths=['src/awf/new_name.py', 'src/awf/old_name.py']`.

Focused validation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passed: `10 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passed.

Full AWF/GitHub validation was not run inside the agent phase per the workspace
contract; AWF owns broad validation and merge gating after agent completion.
