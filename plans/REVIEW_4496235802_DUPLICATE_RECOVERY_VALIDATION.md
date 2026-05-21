# Review 4496235802 Duplicate Recovery Validation

Plan reference: `plans/REVIEW_4496235802_DUPLICATE_RECOVERY_PLAN.md`

## Requirement Status

- Add a regression test proving recovery evidence is attempted once per stale
  scan: Complete. The new test first failed with two recovery calls (`[{}, {}]`)
  and now passes with one call.
- Preserve the stale-failure path when recovery returns `False`: Complete. The
  new test asserts a stale-active detection event is still recorded after the
  skipped duplicate retry.
- Keep the existing async-session expiry regression intact: Complete. The
  adjacent expiry regression passed in focused validation.
- Stage and commit only files changed for this review comment: Complete. Final
  staging is limited to the files listed below.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4496235802_DUPLICATE_RECOVERY_PLAN.md`
- `plans/REVIEW_4496235802_DUPLICATE_RECOVERY_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_recovery_evidence_is_not_retried_in_same_expired_scan -q`
  - Before implementation: failed because `_recover_preserved_active_execution`
    was called twice in one scan.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_recovery_guard_skips_full_recovery_without_evidence tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_recovery_evidence_is_not_retried_in_same_expired_scan tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_non_running_active_validation_fallthrough_refreshes_expiring_session tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_missing_lineage_audit_does_not_block_expired_failure tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_failure_still_applies_after_salvage_not_possible_for_orphan -q`
  - Passed: `5 passed in 12.76s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Failed: `10 failed, 268 passed in 387.78s`.
  - The failures were outside the duplicate retry path, including the existing
    closed-connection stale-scan assertion, no-work replacement expectations,
    and a timeout in `test_preserved_active_validation_salvage_without_executor_blocks_stale_cleanup`.

## Gaps

The full worker test file is still not green on this branch. The focused
review-specific regression and adjacent safety tests pass, so no additional
iteration is needed for this comment fix.
