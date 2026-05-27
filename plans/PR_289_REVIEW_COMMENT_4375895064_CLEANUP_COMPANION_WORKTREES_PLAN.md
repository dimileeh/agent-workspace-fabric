# PR 289 Review Comment 4375895064 Cleanup Companion Worktrees Plan

## Problem Statement

Review-level comment `4375895064` reports that cleanup test doubles accept
`companion_worktrees` but discard the value before recording cleanup calls.
That prevents focused lifecycle/control tests from asserting that companion
worktree cleanup targets reached the cleaner.

## Scope

- Update only the cited test helpers and assertions.
- Preserve existing cleanup behavior and production code.
- Add focused regression assertions for cleaner call capture.
- Use targeted local checks only; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Requirements Checklist

- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py`
  records `companion_worktrees` in `CleanupCall`.
- `tests/unit/service/test_controls_parts/test_controls_part_001.py` records
  `companion_worktrees` in both cleaner call dictionaries.
- Focused assertions prove non-empty companion worktree targets are captured
  where practical, and sequenced cleaner calls include the field.
- Validation uses narrow pytest and lint checks for the touched tests only.

## Implementation Steps

1. Add failing focused assertions that expect companion worktree cleanup targets
   in recorded cleaner calls.
2. Update the cited cleaner test doubles to preserve `companion_worktrees`.
3. Run the focused tests that cover the changed helpers.
4. Run focused Ruff checks on the touched test files.
5. Record validation evidence in
   `plans/PR_289_REVIEW_COMMENT_4375895064_CLEANUP_COMPANION_WORKTREES_VALIDATION.md`.
