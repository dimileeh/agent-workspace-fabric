# Review 4496235802 Duplicate Recovery Plan

## Problem Statement and Scope

Address the still-actionable duplicate recovery portion of review comment
`issue:4496235802` for preserved-active execution recovery. The existing branch
already contains regression coverage for the post-commit async ORM refresh path,
so this plan is limited to avoiding a second
`_recover_preserved_active_execution` call in the same stale scan after recovery
evidence has already been attempted.

## Requirements Checklist

- Add a regression test proving recovery evidence is attempted once per stale
  scan, even when the preserved-active runtime is expired.
- Preserve the existing stale-failure path when recovery returns `False`; do not
  make salvage-not-possible evidence block cleanup forever.
- Keep the existing async-session expiry regression intact.
- Stage and commit only the files changed for this review comment.

## Implementation Steps

1. Add the focused failing test in `tests/unit/control/test_worker.py`.
2. Update `_recover_stale_active_execution` to remember whether
   preserved-active recovery already ran before runtime inspection.
3. Skip the expired-preservation retry only when that same scan already
   attempted recovery.
4. Run the focused regression, adjacent preserved-active recovery tests, ruff,
   and the broader worker test file if practical.
5. Record validation evidence in
   `plans/REVIEW_4496235802_DUPLICATE_RECOVERY_VALIDATION.md`.

## Verification Commands and Pass Criteria

- Focused new regression fails before implementation and passes after.
- Adjacent preserved-active recovery tests pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
- Full `tests/unit/control/test_worker.py` result is recorded; any unrelated
  pre-existing failures are documented without broadening this fix.
