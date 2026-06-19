# Batch Empty Directory Check-Ignore Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6KxRdu` reports that validation worktree cleanup runs `git check-ignore` once per empty-directory cleanup candidate. The fix is scoped to batching ignore probes for `_remove_empty_untracked_dirs` so large generated empty-directory trees do not require thousands of Git processes.

## Requirements Checklist

- Preserve existing cleanup behavior for empty untracked directories.
- Preserve ignored-directory protection, including wildcard-ignored empty directories.
- Preserve failure behavior: if `git check-ignore` fails unexpectedly, do not remove any candidate directories.
- Use one `git check-ignore --stdin` probe for the cleanup candidate batch.
- Add focused regression coverage for the batched cleanup behavior.

## Implementation Steps

1. Add a batch helper that accepts multiple candidate paths and returns the subset matched by `git check-ignore --stdin`.
2. Update `_remove_empty_untracked_dirs` to collect candidates first, batch-probe them, and remove only non-ignored candidates.
3. Update existing failure-focused tests for the new batched command shape.
4. Add a regression test that verifies many empty-directory cleanup candidates use one `check-ignore` call.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_batch_check_ignore_candidates tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir -q`
  - Passes, proving focused cleanup failure, batching, and wildcard-ignore behavior.

Full AWF/GitHub validation remains managed by AWF after agent completion.
