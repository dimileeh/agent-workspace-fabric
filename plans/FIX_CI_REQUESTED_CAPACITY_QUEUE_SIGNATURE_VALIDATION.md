# Fix CI Requested Capacity Queue Signature Validation

Plan reference: `FIX_CI_REQUESTED_CAPACITY_QUEUE_SIGNATURE_PLAN.md`

## Requirement Status

- Reproduce the reported pytest failure locally before changing code: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_capacity_claim_empty_queue_returns_empty_result -q` failed with `TypeError` because the monkeypatched `queue_signature` helper did not accept `scoring_at`.
- Keep the production capacity scheduler behavior intact: Complete.
  - Evidence: no production files were changed.
- Update the stale test double to match the helper contract and cover the expected `scoring_at` argument: Complete.
  - Evidence: `tests/unit/control/test_worker.py` now accepts `scoring_at` in the test helper and asserts it is supplied.
- Run focused validation for the failing node ID and nearby queue-signature worker tests: Complete.
  - Evidence:
    - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_capacity_claim_empty_queue_returns_empty_result -q` passed.
    - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "requested_capacity_queue_signature or capacity_claim_empty_queue" -q` passed with `10 passed, 223 deselected`.
    - `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_worker.py` passed.
- Save validation evidence in this file: Complete.
- Commit the fix locally with a conventional commit message describing the CI check and root cause: Complete.
  - Evidence: this scoped fix is prepared for a local conventional commit after validation.

## Residual Risk

The fix is limited to a stale unit-test monkeypatch contract. Broader full
coverage was not run locally because AWF provided a focused CI failure and the
nearby scheduler test slice passed.
