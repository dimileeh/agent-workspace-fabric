# Address Review Comment 3331116339 Plan

## Problem Statement and Scope

Review feedback highlights a cleanup regression path in
`cleanup_validation_worktree_side_effects`:
when tracked files are restored with `git restore --source`, the worktree can remain
dirty and the function returns before checking whether `HEAD` changed. That can
leave the workspace on the validation-authored commit even though `restore_ref` is
available.

This change is limited to worktree cleanup behavior and unit tests for that module.

## Requirements Checklist

- Preserve existing cleanup behavior for tracked/untracked restore flows that finish cleanly.
- Add a regression test where:
  - tracked files are reported dirty before cleanup,
  - `git restore --source` succeeds,
  - post-clean verification is still dirty, and
  - `HEAD` differs from `restore_ref`.
- Ensure the function attempts rollback and reports `git reset --hard` failure/success
  outcome in that scenario.
- Run focused tests for `tests/unit/runtime/test_validation_worktree.py` only.
- Create/update plan and validation docs per repository protocol.

## Implementation Steps

1. Update `cleanup_validation_worktree_side_effects` so a head-rollback check is
   performed before returning dirty post-cleanup failures.
2. Add a regression unit test in
   `tests/unit/runtime/test_validation_worktree.py` for the tracked-restore-dirty +
   head-changed case.
3. Run a focused test command that includes the new case.
4. Record results in `plans/ADDRESS_REVIEW_3331116339_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
- All targeted assertions in the new/updated tests pass and broader AWF/CI validation
  is left for post-agent workflows.
