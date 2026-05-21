# Requested Capacity Profile Signature Validation

Plan reference:
`plans/REQUESTED_CAPACITY_PROFILE_SIGNATURE_PLAN.md`

## Requirement Status

- Regression test for resolved-profile-only queue signature changes: Complete.
  Added
  `test_requested_capacity_queue_signature_changes_when_resolved_profile_changes`
  in `tests/unit/control/test_worker.py`.
- Include `resolved_profile` in PostgreSQL and non-PostgreSQL queue signature
  digests: Complete. Updated `_requested_capacity_queue_signature` and
  `_requested_capacity_queue_digest_payload` in `src/awf/control/worker.py`.
- Preserve existing resume cursor behavior for unchanged signatures: Complete.
  The signature tuple shape and existing cursor comparison flow are unchanged;
  only the digest payload now includes the profile snapshot.
- Keep edits focused: Complete. Changes are limited to the scheduler signature,
  its tests, and required plan/validation records.

## Evidence

- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_changes_when_resolved_profile_changes -q`
  failed because the before/after signatures were identical.
- Confirmed the regression passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_changes_when_resolved_profile_changes -q`
  passed.
- Ran affected unit suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed with 219 tests.
- Ran lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.

## Gaps

None.
