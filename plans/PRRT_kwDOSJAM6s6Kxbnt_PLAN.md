# PRRT_kwDOSJAM6s6Kxbnt Plan

## Problem Statement and Scope

The review thread reports that snapshot-mode empty-directory detection calls
`git check-ignore` once per candidate when `ignore_check_ignored_empty_dirs` is
enabled. The scope is limited to batching that ignore probe while preserving the
existing snapshot behavior for empty directories, ignored paths, and gitlink
boundaries.

## Requirements Checklist

- Add a focused regression test showing snapshot mode issues one batched
  `git check-ignore --stdin -z` call for multiple empty directory candidates.
- Update `_snapshot_empty_untracked_dirs` to collect empty directory candidates
  and batch ignore checks through `_ignored_paths`.
- Preserve existing behavior for non-ignored empty directories, ignored empty
  directories, and failure propagation from `git check-ignore`.
- Run targeted tests for the changed validation worktree behavior only.

## Implementation Steps

1. Add a regression test near the existing validation worktree batching tests.
2. Refactor snapshot collection to gather candidates first, then filter ignored
   candidates with a single `_ignored_paths` call when ignore checks are enabled.
3. Run the new regression test and nearby wildcard-ignored snapshot tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_snapshot_empty_untracked_dirs_batch_check_ignore_candidates -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_ignores_wildcard_ignored_empty_dir_when_opted_in tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_fails_when_check_ignore_fails -q`
  passes.
- Full AWF/GitHub validation is intentionally left to the AWF post-agent
  validation pipeline per workspace contract.
