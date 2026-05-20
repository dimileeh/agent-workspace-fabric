# PR #272 Review Comment 4496235802 Validation

Plan reference: `plans/PR272_REVIEW_4496235802_PLAN.md`

## Requirement Status

- Confirm validating/pushing rewind does not orphan pre-existing pending or
  running validate/push operations: Complete.
  Existing implementation cancels superseded active validate/push operations in
  `_request_preserved_active_validation`; the preserved-active worker slice
  includes the regression asserting the original operation is cancelled.
- Add an audit trail for missing task/attempt lineage before preservation grace
  expiry: Complete.
  Added a non-terminal `workspace.active_execution_salvage_blocked` event and
  `runtime_preserved_salvage_blocked` subphase.
- Preserve stale-active cleanup after preservation grace expires: Complete.
  The blocked audit event is not part of the stale-failure blocking salvage
  checks, and the new expired-lineage regression verifies the workspace still
  fails after grace expiry.
- Replace the two sequential active validation recovery queries with one
  `IN`-filtered query: Complete.
  `_has_active_preserved_validation_recovery` now selects active pending/running
  validation recovery payloads in one query.
- Add or update focused regression tests before implementation: Complete.
  Added three failing-first regressions in `tests/unit/control/test_worker.py`.
- Run the narrowest validation commands that prove the changes: Complete.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "missing_attempt_lineage_records_audit or missing_lineage_audit_does_not_block_expired_failure or validation_recovery_lookup_uses_single_active_query"`
  - Failed before implementation with the expected missing audit/query failures.
  - Passed after implementation: `3 passed, 202 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active or stale_active_failure_still_applies_after_salvage_not_possible_for_orphan"`
  - Passed: `19 passed, 186 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Files Changed

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PR272_REVIEW_4496235802_PLAN.md`
- `plans/PR272_REVIEW_4496235802_VALIDATION.md`
