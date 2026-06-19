# Plan: fix check-ignore error handling (PR #606 thread PRRT_kwDOSJAM6s6KIgh1)

## Problem statement and scope

Reviewer points out that `_is_ignored_path` in `src/awf/runtime/validation_worktree.py` swallows `git check-ignore` failures. `git check-ignore` exit codes:

- `0` — path matches an ignore rule (stdout contains the path).
- `1` — path does not match any ignore rule.
- `>=2` / non-zero other than a clean 1 — command failure (bad config, bad arguments, git error, etc.).

Current implementation returns `result.returncode == 0 and result.stdout.strip() != ""`, so a real command failure (e.g. exit `128`) is treated the same as "not ignored" and returns `False`. That lets empty-directory cleanup remove wildcard-ignored empty directories like `cache/`, contradicting the preserve-ignored-empty-dir behavior and mirroring the unsafe "probe failed, proceed anyway" pattern that was already fixed for `_gitlink_paths`.

Lines involved: `_is_ignored_path` itself, and its two callers at lines 357 (`_remove_empty_untracked_dirs`) and 414 (`_snapshot_empty_untracked_dirs`).

## Requirements checklist

1. `_is_ignored_path` distinguishes three outcomes:
   - ignored
   - not ignored
   - probe failed (command failure / unexpected output)
2. Empty-directory helpers (`_remove_empty_untracked_dirs`, `_snapshot_empty_untracked_dirs`) treat a failed check-ignore probe as a **failure**, not as "not ignored".
3. A failed probe from `check_validation_worktree_clean` surfaces as `VALIDATION_WORKTREE_STATUS_FAILED` (infrastructure error), similar to the existing `_GitlinkLookupError` handling.
4. A failed probe during `cleanup_validation_worktree_side_effects` cleanup also fails safely instead of removing ignored directories.
5. Add focused regression tests covering:
   - a failing `git check-ignore` command prevents removal/surfacing of a wildcard-ignored empty directory;
   - a clean "not ignored" (exit 1) still allows removal/surfacing.
6. Do not change behavior for the normal success paths; keep changes minimal and scoped to the reported issue.

## Implementation steps

1. Add a `_IgnoreCheckError(Exception)` class carrying `stderr`.
2. Change `_is_ignored_path` to:
   - return `True` when `returncode == 0` and stdout non-empty;
   - return `False` when `returncode == 1`;
   - raise `_IgnoreCheckError` for any other return code, including stderr.
3. Update `_remove_empty_untracked_dirs` to catch `_IgnoreCheckError` and propagate it upward, aborting empty-directory cleanup (so the directory is not removed). Because `check_validation_worktree_clean` already catches `_GitlinkLookupError` for the same block, expand the except clause to also catch `_IgnoreCheckError`.
4. Update `_snapshot_empty_untracked_dirs` similarly.
5. Add unit tests that monkeypatch `subprocess.run` to return a non-zero, non-1 exit for `check-ignore` and assert that the directory is preserved and that the high-level check reports an infrastructure failure.

## Verification commands and pass criteria

- Run targeted tests for the touched file:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
- Run focused lint/type checks on the touched module:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py`
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
- Pass criteria: tests pass, lint clean, typecheck clean.

Note: broad validation suites are owned by AWF/GitHub CI per the workspace contract; run only these narrow checks.
