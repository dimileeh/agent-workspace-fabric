# Review 4495131102 Capacity Signature Validation

Plan reference: `plans/review_4495131102_capacity_signature_PLAN.md`

## Requirement Status

- Bound SQLite requested-capacity queue signature scan: Complete.
  `src/awf/control/worker.py` applies a 500-row limit to the non-PostgreSQL
  snapshot query.
- Preserve single-snapshot SQLite fallback behavior: Complete.
  The existing single-read regression remains, and the bounded query test uses
  the same one-execute path.
- Make PostgreSQL `NULL` digest fallback explicit: Complete.
  The aggregate digest result now uses an empty-string fallback before string
  coercion.
- Add regression tests for both review observations: Complete.
  `tests/unit/control/test_worker.py` covers the SQLite `LIMIT 500` query and
  the PostgreSQL `ids_digest is None` aggregate result.
- Keep changes scoped: Complete.
  Changes are limited to the worker helper, focused tests, and required
  plan/validation records.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_sqlite_bounds_snapshot_scan tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_postgres_null_digest_uses_empty_string -q`
  failed before implementation on the missing SQLite limit and `"None"` digest.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_sqlite_bounds_snapshot_scan tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_queue_signature_postgres_null_digest_uses_empty_string -q`
  passed after implementation: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed: 222 passed.
