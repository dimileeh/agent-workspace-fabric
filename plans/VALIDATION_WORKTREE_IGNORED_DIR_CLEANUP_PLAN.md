# Validation Worktree Ignored Directory Cleanup Plan

## Problem statement and scope

The PR review thread reports that validation cleanup removes new ignored files
under a preserved ignored root, but can leave empty directories created only to
hold those files. The final cleanliness check intentionally filters the
preserved ignored root, so cleanup can report success while leaving filesystem
state that is not in the submitted commit.

Scope is limited to `src/awf/runtime/validation_worktree.py` and its unit tests.
No branch switching, pushing, broad validation, full coverage, or CI-equivalent
commands will be run from this workspace.

## Requirements checklist

- Add a regression test where cleanup removes a new ignored file below a nested
  directory under a preserved ignored root and also removes the now-empty nested
  directory.
- Preserve the ignored root itself and avoid deleting non-empty directories.
- Keep existing ignored-file safety checks intact: deleted or modified baseline
  ignored files must still fail cleanup.
- Keep cleanup failure reporting deterministic if an empty generated directory
  cannot be removed.
- Validate with focused unit tests only; document that broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation steps

1. Add the failing regression in `tests/unit/runtime/test_validation_worktree.py`.
2. Implement targeted empty-directory cleanup after successful `git clean` for
   newly-created ignored paths under preserved ignored roots.
3. Return a cleanup failure with the existing cleanup failure reason if an empty
   generated directory remains but cannot be removed.
4. Run the new focused test, then a small adjacent focused test selection for
   validation worktree cleanup behavior.
5. Create the validation document with requirement status and focused evidence.
