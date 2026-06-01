# COMMENT_3333019955_EMPTY_IGNORED_ROOT Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GEJ9l` reports that validation cleanup does
not detect deletion of a pre-validation ignored root when that root was empty.
Git status can report an ignored root such as `build/`, while
`git ls-files --others --ignored` has no file entries for an empty directory.
Cleanup currently compares only the file snapshot and can therefore accept a
workspace where setup-owned ignored directory state disappeared.

Scope is limited to `src/awf/runtime/validation_worktree.py` cleanup behavior
and focused unit tests for that helper.

## Requirements Checklist

- Add a regression test for a deleted pre-existing ignored root with an empty
  ignored file snapshot.
- Preserve existing protections for deleted or modified ignored file snapshot
  entries.
- Fail cleanup with `VALIDATION_WORKTREE_CLEANUP_FAILED` before accepting
  validation evidence when a baseline ignored root is no longer reported.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for the
  touched runtime helper.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_validation_worktree.py`
   that simulates `!! build/` before validation, an empty ignored snapshot, and
   no current ignored root after validation cleanup begins.
2. Confirm the new test fails against the current implementation.
3. Update `cleanup_validation_worktree_side_effects` to compare pre-validation
   ignored roots against the current ignored roots captured by the cleanup
   status check, while treating file entries under a root as evidence that the
   root still exists.
4. Run the targeted validation-worktree test selection.
5. Record validation evidence in the matching validation artifact.

## Verification Commands And Pass Criteria

- Focused ignored-root/ignored-snapshot regression tests pass.
- Focused Ruff check for the touched files passes.
- Full AWF/GitHub validation remains delegated to AWF after agent completion,
  per workspace contract.

## Assumptions/Changes

- A full `tests/unit/runtime/test_validation_worktree.py` run was attempted
  after the fix and exposed existing failures outside this review-thread scope,
  including contradictory expectations for untracked cleanup without
  `restore_ref`. The scoped verification remains the new regression, nearby
  ignored-snapshot cleanup tests, and Ruff on touched files.
