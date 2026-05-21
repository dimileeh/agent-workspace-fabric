# Comment 4508578544 Plan

## Problem Statement and Scope

PR review comment `issue:4508578544` flags that `src/awf/runtime/ownership.py`
imports the private `awf.node.git_manager._linked_worktree_git_dir` helper across
module boundaries. The fix should promote that helper to a public API in
`git_manager.py` and update outside-module callers and tests to use the public
name.

## Requirements Checklist

- Add or update regression coverage so callers exercise the public
  `linked_worktree_git_dir` helper instead of the private helper.
- Replace cross-module use of `_linked_worktree_git_dir` with the public
  `linked_worktree_git_dir` API.
- Preserve existing ownership-repair behavior, including relative `.git` pointer
  resolution and unreadable or malformed `.git` handling.
- Keep the change scoped to the review comment and avoid unrelated refactors.
- Commit the completed fix locally with a conventional commit message for this
  review comment.

## Implementation Steps

1. Update targeted tests to call and monkeypatch `linked_worktree_git_dir`.
2. Run the targeted tests to confirm the public API is not implemented yet.
3. Rename/promote the helper in `git_manager.py` and update internal call sites.
4. Update `ownership.py` to import and use the public helper.
5. Run targeted unit tests for ownership and git manager helper behavior.
6. Run lint for touched Python areas if feasible.
7. Record validation evidence in `plans/comment_4508578544_VALIDATION.md`.
8. Stage only changed files and commit locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/runtime/test_ownership.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py src/awf/runtime/ownership.py tests/unit/node/test_git_manager.py tests/unit/runtime/test_ownership.py`
  passes.
- `git diff --check` passes.
