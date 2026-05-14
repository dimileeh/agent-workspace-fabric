# Review 4445667428 Validation

Plan reference: `plans/REVIEW_4445667428_PLAN.md`

## Requirement Status

- Add service-level coverage for the shared row-restoration helper before implementation: Complete.
  - Added `test_restore_primary_failure_row_fields_preserves_bounded_primary_message` in `tests/unit/service/test_failure_causality.py`.
  - Confirmed the test failed before implementation with `ImportError: cannot import name 'restore_primary_failure_row_fields'`.
- Keep existing failure-causality behavior unchanged for worker stale execution, runtime stranding, and control cleanup failures: Complete.
  - Worker and controls now call the shared helper without changing transition payload logic.
- Replace worker/control local helper definitions with imports from `failure_causality.py`: Complete.
  - Added `restore_primary_failure_row_fields` in `src/awf/service/failure_causality.py`.
  - Imported it in `src/awf/control/worker.py` and `src/awf/service/controls.py`.
  - Removed both duplicate local helper definitions.
- Remove `_claim_recheck_conditions` and its only test/import reference without weakening claim-staleness coverage: Complete.
  - Removed `_claim_recheck_conditions` from `src/awf/control/worker.py`.
  - Removed its import and assertion from `tests/unit/control/test_worker.py`; the test still covers `_candidate_claim_is_stale` for non-runtime statuses.
- Commit the fix locally on the current AWF-managed branch: Complete.
  - This validation file is included in the local review-comment fix commit.
- Print the required `AWF-VERDICT` line after the fix is complete: Complete.
  - The verdict is emitted after the local commit as required by the AWF comment-handling contract.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `src/awf/control/worker.py`
- `src/awf/service/controls.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4445667428_PLAN.md`
- `plans/REVIEW_4445667428_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q` failed before implementation as expected because the new helper was missing.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q` passed: 24 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q` passed: 176 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle.py -q` passed: 50 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/control/test_worker.py tests/unit/service/test_failure_causality.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

No implementation gaps remain.
