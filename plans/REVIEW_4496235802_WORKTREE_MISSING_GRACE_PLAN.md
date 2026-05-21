# Review 4496235802 Worktree Missing Grace Plan

## Problem Statement

The review reports that preserved-active recovery treats a missing worktree as
immediately replaceable `no_work`, even while the active execution preservation
grace period is still open. That can cancel a live agent if filesystem access is
temporarily unavailable during worker restart recovery.

## Scope

Change only the preserved-active `no_work` recovery decision and focused unit
coverage. Do not alter branch management, PR monitor recovery, stale cleanup, or
expired-grace replacement behavior.

## Requirements Checklist

- Add a regression proving `no_work / worktree_missing` records
  `SALVAGE_BLOCKED` and keeps the original workspace while preservation has not
  expired.
- Keep `clean_branch_not_ahead` behavior unchanged.
- Keep expired `no_work` replacement behavior available.
- Run the narrow affected pytest selection and ruff for changed files.
- Commit the scoped fix locally.

## Implementation Steps

1. Add a missing-worktree preserved-active recovery test near the existing
   clean no-work grace tests.
2. Confirm the new test fails before the production change.
3. Treat `worktree_missing` like `clean_branch_not_ahead` for non-expired
   preservation windows.
4. Re-run the focused tests and lint.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_missing_worktree_waits_for_preservation_grace or preserved_active_clean_worktree_without_commits_waits_for_preservation_grace or preserved_active_clean_worktree_without_commits_replaces_after_grace"`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`

Pass criteria: the targeted regression and adjacent no-work grace tests pass,
and ruff reports no issues.
