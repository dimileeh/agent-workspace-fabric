# Review 4496235802 Worktree Missing Grace Validation

Plan reference:
`plans/REVIEW_4496235802_WORKTREE_MISSING_GRACE_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `no_work / worktree_missing` records
  `SALVAGE_BLOCKED`, keeps the original workspace running, and does not create a
  replacement while preservation grace is open.
- Complete: `clean_branch_not_ahead` remains covered by the adjacent existing
  grace test.
- Complete: Expired no-work replacement behavior remains covered by the
  adjacent existing after-grace test.
- Complete: Narrow pytest and ruff validation passed.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4496235802_WORKTREE_MISSING_GRACE_PLAN.md`
- `plans/REVIEW_4496235802_WORKTREE_MISSING_GRACE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_missing_worktree_waits_for_preservation_grace"`
  - Before implementation: failed because a replacement workspace was created
    during the open grace period.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_missing_worktree_waits_for_preservation_grace or preserved_active_clean_worktree_without_commits_waits_for_preservation_grace or preserved_active_clean_worktree_without_commits_replaces_after_grace"`
  - Passed: 3 passed, 267 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.

## Gaps

None.
