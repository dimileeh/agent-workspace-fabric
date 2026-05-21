# PRRT_kwDOSJAM6s6Drjx1 Worktree Root Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Drjx1_WORKTREE_ROOT_PLAN.md`

## Requirement Status

- Complete: Classify `worktree_root_unavailable` as retryable failed classification.
  - Evidence: `src/awf/control/worker.py` now returns `state="failed"` for missing preserved worktree roots.
- Complete: Preserve expired-grace operator recovery behavior.
  - Evidence: `test_preserved_active_unknown_worktree_root_requires_operator_recovery_after_grace` still asserts operator-required recovery after grace expires, with the classification state updated to `failed`.
- Complete: Add/update regression coverage for during-grace salvage blocking.
  - Evidence: `test_preserved_active_unknown_worktree_root_retries_during_grace` asserts `ACTIVE_EXECUTION_SALVAGE_BLOCKED`, no operator-required event, and failed classification payload for `worktree_root_unavailable`.
- Complete: Keep scope limited to worker behavior, unit tests, and plan/validation docs.
  - Evidence: changed files are limited to `src/awf/control/worker.py`, `tests/unit/control/test_worker.py`, and the two plan documents.

## Verification

- Failing-first check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'worktree_root or unknown_worktree_root'`
  - Result before implementation: failed because `worktree_root_unavailable` was still `ambiguous` and during-grace recovery recorded operator-required.
- Focused pass:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'worktree_root or unknown_worktree_root'`
  - Result after implementation: passed, `3 passed, 266 deselected`.
- Adjacent salvage pass:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'git_status_failure or worktree_root or unknown_worktree_root'`
  - Result: passed, `5 passed, 264 deselected`.
- Lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.

## Gaps

None.
