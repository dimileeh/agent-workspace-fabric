# Review 4495131102 PostgreSQL Queue Signature Limit Validation

Plan reference: `plans/REVIEW_4495131102_POSTGRES_QUEUE_SIGNATURE_LIMIT_PLAN.md`

## Requirement Status

- Bound PostgreSQL requested-capacity queue signature aggregation: Complete.
  `src/awf/control/worker.py` now builds a 500-row requested-queue frontier
  subquery and runs the PostgreSQL aggregate digest over that bounded sample.
- Preserve existing signature fields: Complete.
  The aggregate still returns sampled count, latest update time, latest created
  time, max workspace id, and digest.
- Keep PostgreSQL `NULL` digest fallback behavior unchanged: Complete.
  The return path still converts a missing digest to an empty string.
- Add regression coverage for the capped PostgreSQL aggregate: Complete.
  `tests/unit/control/test_worker.py` now compiles the PostgreSQL signature
  statement and asserts the aggregate reads from a subquery with `LIMIT 500`.
- Keep changes scoped and preserve AWF git constraints: Complete.
  Changes are limited to the worker helper, focused tests, and this
  plan/validation pair.

## Evidence

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_postgres_bounds_aggregate_scan -q`
  failed because the compiled PostgreSQL aggregate query had no `LIMIT 500`.
- After implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_postgres_bounds_aggregate_scan -q`
  passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"`
  passed: 8 passed, 224 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
