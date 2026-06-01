# Review PRRT_kwDOSJAM6s6GDo7r Cleanup Head Rollback Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GDo7r` reports that
`cleanup_validation_worktree_side_effects` returns immediately when `git restore`
or `git clean` fails. Those direct returns skip the shared HEAD verification
rollback used by other cleanup failure paths, so a validation-authored commit can
remain checked out when cleanup fails.

Scope is limited to `src/awf/runtime/validation_worktree.py` and focused unit
coverage in `tests/unit/runtime/test_validation_worktree.py`.

## Requirements Checklist

- Add regression coverage proving failed `git restore` rolls back HEAD when
  validation moved HEAD.
- Add regression coverage proving failed `git clean` rolls back HEAD when
  validation moved HEAD.
- Preserve existing cleanup failure behavior when HEAD did not move or no
  `restore_ref` was captured.
- Keep validation focused; full AWF/GitHub validation remains owned by AWF after
  agent completion.
- Commit this thread fix locally without switching branches or pushing.

## Implementation Steps

1. Add two focused async unit tests in `tests/unit/runtime/test_validation_worktree.py`.
2. Run those tests before implementation to confirm they fail for the reported
   gap.
3. Update the failed `git restore` and `git clean` cleanup returns to pass
   through the existing `_return_after_head_verification` helper.
4. Run the focused unit tests and a targeted lint check for the touched files.
5. Save validation results in the matching validation document.
6. Stage only changed files and commit with a conventional message referencing
   the review thread.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passes.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation after
  agent completion.

## Assumptions/Changes

- During validation, the whole `tests/unit/runtime/test_validation_worktree.py`
  file exposed pre-existing, contradictory expectations for untracked cleanup
  when `restore_ref` is missing. This review thread is limited to failed
  cleanup commands when a `restore_ref` exists and validation moved HEAD, so
  local pass criteria are narrowed to the new rollback regressions plus the
  adjacent tracked-restore stderr regression and targeted ruff.
