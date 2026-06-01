# PRRT_kwDOSJAM6s6GKowx Literal Restore Pathspecs Plan

## Problem Statement And Scope

An unresolved review thread reports that validation-worktree cleanup restores
dirty tracked files with pathspec-style arguments. A tracked filename that is
also Git pathspec syntax, such as `:(glob)foo`, can make `git restore` fail or
target the wrong path, leaving validation side effects behind.

Scope is limited to tracked-file restore cleanup in
`src/awf/runtime/validation_worktree.py` and focused regression coverage. No
broad AWF/GitHub validation will be run in the agent phase.

## Requirements Checklist

- Add a regression test showing tracked validation dirt with a pathspec-magic
  filename is restored literally.
- Use literal pathspec handling for tracked restore cleanup, matching the
  existing untracked `git clean` cleanup path.
- Preserve existing cleanup behavior for ordinary tracked and untracked paths.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Add a focused real-Git unit test in
   `tests/unit/runtime/test_validation_worktree.py` for a tracked
   `:(glob)foo` file dirtied by validation.
2. Run the new test before the code change and confirm it fails.
3. Prefix the tracked restore command with `--literal-pathspecs`.
4. Run the new regression plus nearby validation-worktree cleanup tests.

## Verification Commands And Pass Criteria

- Red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k tracked_pathspec_magic_path_literally`
- Focused green check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k "tracked_pathspec_magic_path_literally or restores_tracked_path_under_ignored_root or cleans_generated_ignored_metachar_path_literally"`
- Pass criteria: the new regression fails before the implementation, passes
  after it, nearby cleanup behavior remains green, and broad validation remains
  delegated to AWF/GitHub after agent completion.
