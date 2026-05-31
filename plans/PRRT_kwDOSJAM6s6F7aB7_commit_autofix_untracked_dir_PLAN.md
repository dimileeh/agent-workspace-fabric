# PRRT_kwDOSJAM6s6F7aB7 Commit Autofix Untracked Directory Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F7aB7` reports that the PR monitor
pre-commit autofix retry compares post-hook dirty paths against the initial
operation dirty paths with exact path matching. When Git initially reports a
brand-new directory as `?? newdir/`, a later hook-modified staged file can
appear as `AM newdir/file.py`; the exact subset check treats that deterministic
hook edit as outside the operation and skips the retry.

Scope is limited to the PR monitor commit autofix helper and focused unit
coverage for directory-scoped untracked operation paths.

## Requirements Checklist

- Add a regression test showing an initial untracked directory operation path
  allows a later hook-modified file contained in that directory.
- Preserve the operation-scope guard for dirty paths outside the initial
  operation, including paths that only share a string prefix with the directory
  name.
- Preserve the repair-path guard so only deterministic hook-reported worktree
  modifications are restaged.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add a targeted async unit test in
   `tests/unit/runtime/test_pr_monitor_commit_autofix.py` with
   `operation_dirty_paths=("newdir/",)` and post-hook status
   `AM newdir/file.py`.
2. Confirm the regression fails against the current implementation when
   practical.
3. Update `src/awf/runtime/pr_monitor_runner/commit_autofix.py` so the
   operation-scope safety check treats operation paths ending in `/` as
   directory scopes for contained paths.
4. Run the focused regression and touched unit test file.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_hook_modified_files_inside_untracked_operation_directory -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Pass criteria: focused tests and touched-file lint pass. Full AWF/GitHub
validation remains owned by AWF after agent completion.
