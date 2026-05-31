# PRRT_kwDOSJAM6s6F67kW Commit Autofix Rename Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F67kW` reports that PR monitor pre-commit
autofix retry rejects a dirty rename when Git reports `RM old -> new` and the
deterministic hook output names only the renamed file. The fix is scoped to
`src/awf/runtime/pr_monitor_runner/commit_autofix.py` and targeted unit
coverage for that retry behavior.

## Requirements Checklist

- Add a regression test that reproduces a worktree-modified rename reported as
  `RM old -> new`.
- Preserve the existing safety rule that unrelated worktree-modified paths are
  rejected.
- Treat only the new side of a worktree-modified rename as needing a hook repair
  path match.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add a targeted unit test in
   `tests/unit/runtime/test_pr_monitor_commit_autofix.py`.
2. Confirm the new test fails against the current implementation when practical.
3. Update `_worktree_modified_paths_from_porcelain` so rename/copy porcelain
   records with unstaged worktree changes return the destination path for the
   worktree-modified side.
4. Run the focused unit test file or specific tests that cover the changed
   behavior.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passes.
- If the initial regression is run before implementation, it should fail because
  the retry is considered unsafe.
