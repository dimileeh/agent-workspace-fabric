# Queue Signature SQLite Single Read 4495131102 Validation

Plan reference: `plans/QUEUE_SIGNATURE_SQLITE_SINGLE_READ_4495131102_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving the non-PostgreSQL queue signature
  path performs one `execute` call for a single queue snapshot.
  - Evidence:
    `test_requested_capacity_queue_signature_sqlite_reads_queue_once` uses a
    fake SQLite session that raises on a second `execute`; it failed before the
    implementation and passes after the fix.
- Complete: Keep the signature tuple shape unchanged.
  - Evidence: `_RequestedCapacityQueueSignature` is unchanged and the fallback
    still returns `(count, latest_updated_at, latest_created_at, max_id,
    digest)`.
- Complete: Preserve existing signature contents.
  - Evidence: the fallback now computes count, max timestamps, max workspace
    id, and the existing SHA-256 queue-field digest from the single selected row
    set.
- Complete: Keep the PostgreSQL signature path unchanged.
  - Evidence: only the `bind.dialect.name != "postgresql"` branch in
    `src/awf/control/worker.py` changed.

## Commands Run

- Expected failing pre-implementation check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "sqlite_reads_queue_once"`
  - Result before implementation: failed with `AssertionError: queue signature fallback must read one snapshot`.
- Focused queue-signature check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"`
  - Result: passed, `4 passed`.
- Static check:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Remaining Gaps

None.
