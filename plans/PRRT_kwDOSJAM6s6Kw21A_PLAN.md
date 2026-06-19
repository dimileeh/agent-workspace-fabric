# PRRT_kwDOSJAM6s6Kw21A Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6Kw21A` reports that `_snapshot_empty_untracked_dirs` suppresses wildcard-ignored empty directories even when `check_validation_worktree_clean` is called with the default `ignore_all_ignored=False`. Ignored paths should remain dirty by default and only be hidden when the caller opts into ignoring ignored paths.

## Scope

- Touch only validation worktree empty-directory snapshot behavior and focused regression tests.
- Preserve the existing `ignore_all_ignored=True` behavior for validation/pre-push checks.
- Do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- [ ] Add a regression proving a wildcard-ignored empty directory is dirty when `ignore_all_ignored=False`.
- [ ] Keep wildcard-ignored empty directories clean when `ignore_all_ignored=True`.
- [ ] Keep AWF agent-runtime ignored roots suppressed unconditionally.
- [ ] Keep the code change minimal and localized.

## Implementation Steps

1. Make `_snapshot_empty_untracked_dirs` accept explicit caller intent for whether gitignore-matched empty dirs should be suppressed.
2. Pass `ignore_all_ignored` from `check_validation_worktree_clean` into the snapshot path.
3. Update direct helper tests that intentionally exercise gitignore suppression to opt in explicitly.
4. Run focused tests for wildcard-ignored validation worktree behavior.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py -q`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
