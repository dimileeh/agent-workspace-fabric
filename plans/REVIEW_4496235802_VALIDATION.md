# Review 4496235802 Validation

Plan reference: `plans/REVIEW_4496235802_PLAN.md`

## Requirement Status

- Verify whether replacement-attempt-missing concern is still present:
  Complete. The worker already records `SALVAGE_BLOCKED` with
  `blocked_reason="replacement_attempt_missing"` when an idempotent existing
  replacement has no attempt record.
- Add or confirm regression coverage for replacement-attempt-missing blocked
  salvage behavior:
  Complete. Existing coverage is
  `test_preserved_active_replacement_missing_existing_attempt_records_blocked`.
- Add failing regression for first committed-work validation recovery when
  slots are disabled after preservation grace:
  Complete. Added
  `test_preserved_active_committed_work_slot_exhaustion_after_grace_marks_not_possible_on_first_scan`.
  It failed before the worker change because recovery returned `True`.
- Update worker recovery flow so a failed first dispatch follows existing
  slot-exhaustion terminal logic:
  Complete. The committed-work validation request branch now records
  `SALVAGE_NOT_POSSIBLE` and returns `False` when preservation grace has
  expired and execution slots are permanently disabled.
- Run narrow tests that prove the review fixes:
  Complete. See command evidence below.
- Commit only changed files:
  Pending at validation-write time; completed after this file was written.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4496235802_PLAN.md`
- `plans/REVIEW_4496235802_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'committed_work_slot_exhaustion_after_grace_marks_not_possible_on_first_scan'`
  - Expected pre-fix result: failed because `_recover_preserved_active_execution`
    returned `True`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'replacement_missing_existing_attempt_records_blocked or committed_work_slot_exhaustion_after_grace_marks_not_possible_on_first_scan or validation_slot_exhaustion_after_grace_does_not_block_stale_failure'`
  - Result: passed, `3 passed, 286 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Remaining Gaps

None for the reviewed issues.
