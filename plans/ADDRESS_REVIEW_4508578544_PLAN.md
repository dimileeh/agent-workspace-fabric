# Address Review 4508578544 Plan

## Problem Statement And Scope

Address the two actionable suggestions from PR review comment `issue:4508578544`:

- Make top-level dangling symlinks in Git ownership repair receive `lchown` instead of being skipped.
- Give the dirty-worktree post-commit-success ownership repair failure path a distinct log event from the post-commit-failed path.

Scope is limited to the reviewed behavior and regression tests.

## Requirements Checklist

- Add or update regression coverage before implementation.
- Preserve missing-path skipping while allowing dangling symlinks through `_chown_targets`.
- Preserve existing `lchown` behavior for non-recursive symlink targets.
- Keep failed-commit ownership repair logging unchanged, including `commit_stderr`.
- Log the post-success ownership repair failure with a distinct event name.
- Run targeted tests covering the changed behavior.
- Commit only the files changed for this review response.

## Implementation Steps

1. Add a unit test proving `_chown_targets` calls `os.lchown` for a dangling symlink.
2. Update the PR monitor regression test to assert the distinct post-success repair failure event.
3. Run the targeted tests and confirm they fail before implementation when practical.
4. Update `_chown_targets` to treat `exists() or is_symlink()` as present.
5. Update the post-success PR monitor warning event name.
6. Re-run targeted tests and any narrow lint/type check justified by the touched files.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::test_chown_targets_uses_lchown_for_dangling_non_recursive_symlink tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_logs_commit_when_post_commit_ownership_repair_fails -q
```

Pass criteria: both targeted tests pass after implementation, and the pre-implementation run demonstrates the expected regression failure where practical.
