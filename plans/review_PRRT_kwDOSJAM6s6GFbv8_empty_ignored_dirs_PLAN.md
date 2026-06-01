# Empty Ignored Directory Cleanup Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GFbv8` reports that validation cleanup can
return success while leaving a validation-created empty directory below a
preserved ignored root, such as `.venv/generated/`. Git status only reports the
ignored root and `git ls-files --others --ignored` reports files, so a new empty
directory is currently invisible to `cleanup_untracked_paths`.

Scope is limited to `src/awf/runtime/validation_worktree.py` and focused unit
tests for validation worktree cleanup. No broad AWF/GitHub validation will be
run inside this agent phase.

## Requirements Checklist

- Add a regression test proving cleanup removes a new empty directory under a
  preserved ignored root even when no generated file is reported by
  `git ls-files`.
- Preserve pre-existing ignored files and directories represented by the
  pre-validation snapshot.
- Keep deletion/modified-file safety behavior for baseline ignored state.
- Run only targeted tests for the changed validation worktree behavior.

## Implementation Steps

1. Add the failing regression in `tests/unit/runtime/test_validation_worktree.py`.
2. Extend ignored snapshot comparison to include empty directories discovered
   from the worktree filesystem below ignored roots.
3. Make empty-directory cleanup consider directory cleanup paths directly, not
   only their parents.
4. Re-run the targeted validation worktree tests touched by this behavior.

## Verification Commands and Pass Criteria

- First run the new regression test and confirm it fails before implementation
  when practical.
- Run the targeted validation worktree unit-test slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
- Pass criteria: targeted tests pass, plan validation documents all
  requirements as complete, and full AWF/GitHub validation remains delegated to
  AWF after agent completion.
