# PRRT_kwDOSJAM6s6F7Z1h Commit Autofix Worktree Rename Column Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F7Z1h` reports that PR monitor pre-commit
autofix retry misses Git porcelain rename/copy records where the worktree
status column, not the index status column, is `R` or `C`. The unsafe unsplit
`old -> new` path can skip a safe deterministic-hook retry and leave the monitor
blocked on `PRE_EXISTING_DIRTY_WORKTREE`.

Scope is limited to the PR monitor commit autofix helper and focused unit
coverage for the affected porcelain parsing behavior.

## Requirements Checklist

- Add a regression test for worktree-column rename/copy porcelain records such
  as ` R old -> new` and ` C old -> new`.
- Preserve the safety rule that only the new side of a worktree-modified
  rename/copy must match deterministic hook repair paths.
- Keep first-column rename handling and unrelated-path rejection behavior
  unchanged.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add targeted async unit coverage in
   `tests/unit/runtime/test_pr_monitor_commit_autofix.py`.
2. Confirm the new regression fails against the current implementation when
   practical.
3. Update `_worktree_modified_paths_from_porcelain` so it splits rename/copy
   porcelain paths when either status column is `R` or `C`.
4. Run the focused regression and touched unit test file.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_worktree_column_rename_and_copy_destination -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Pass criteria: focused tests and touched-file lint pass. Full AWF/GitHub
validation remains owned by AWF after agent completion.
