# PRRT_kwDOSJAM6s6F7Z1h Commit Autofix Worktree Rename Column Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F7Z1h_commit_autofix_worktree_rename_column_PLAN.md`

## Requirement Status

- Complete: Added a regression test for worktree-column rename/copy porcelain
  records ` R old -> new` and ` C old -> new`.
- Complete: Preserved the safety rule that only the destination side of the
  worktree-modified rename/copy needs to match deterministic hook repair paths.
- Complete: Kept first-column rename handling and unrelated-path rejection
  behavior unchanged.
- Complete: Kept validation focused. Full AWF/GitHub validation is managed by
  AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F7Z1h_commit_autofix_worktree_rename_column_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F7Z1h_commit_autofix_worktree_rename_column_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_worktree_column_rename_and_copy_destination -q
```

Pre-fix result: failed for both `worktree-rename` and `worktree-copy` because
`worktree_modified_paths` contained the unsplit `old -> new` path and the retry
was skipped as unsafe.

Post-fix result: passed, `2 passed in 0.70s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Result: passed, `20 passed in 0.77s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Result: passed, `All checks passed!`.
