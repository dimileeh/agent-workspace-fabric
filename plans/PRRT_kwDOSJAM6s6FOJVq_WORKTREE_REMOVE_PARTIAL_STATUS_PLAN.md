# PRRT_kwDOSJAM6s6FOJVq Worktree Remove Partial Status Plan

## Problem Statement and Scope

The default GC worktree remover can report an aggregate `partial` result when a
failed real removal is accompanied only by an idempotent success for a companion
worktree path that did not exist in the GC candidate. That no-op success should
remain visible in target results, but it should not upgrade the aggregate
failure to `partial`.

Scope is limited to `src/awf/service/gc.py` and focused unit coverage for the
default worktree remover.

## Requirements Checklist

- Add a regression test for a failed existing worktree removal plus a missing
  companion worktree no-op success.
- Preserve per-target result reporting, including idempotent success for the
  missing companion.
- Compute aggregate `partial` only when at least one planned existing worktree
  target was successfully removed.
- Keep existing companion and plain-directory skip behavior intact.
- Run only targeted validation; broad AWF/GitHub validation is owned by AWF
  after the agent exits.

## Implementation Steps

1. Add the focused regression in `tests/unit/service/test_gc_more2.py`.
2. Confirm the regression fails against the current aggregate-status logic.
3. Track which default-remover targets represented existing GC paths at plan
   time.
4. Use only those meaningful targets when deciding failed vs partial after
   removal errors.
5. Run the focused GC tests that cover the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -k "default_worktree_remover" -q`
  - Passes after the implementation.
  - Shows the new regression failing before implementation when practical.
