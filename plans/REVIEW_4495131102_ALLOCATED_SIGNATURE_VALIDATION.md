# Review 4495131102 Allocated Signature Validation

Plan reference: `plans/REVIEW_4495131102_ALLOCATED_SIGNATURE_PLAN.md`

## Requirement Status

- Complete: Preserve capacity scheduling correctness and existing cursor
  invalidation semantics for real allocation changes.
  - Evidence: `_AllocatedReservationSignature` still includes workspace count,
    CPU totals, memory totals, disk, and DinD slots; only CPU and memory
    representation changed from raw floats to fixed-point integers.
- Complete: Stop raw float rounding drift from invalidating the resume cursor.
  - Evidence: `test_allocated_reservation_signature_normalizes_float_drift`
    covers equivalent Python-accumulated and aggregate-style totals.
- Complete: Keep the signature comparable with exact tuple equality.
  - Evidence: `_AllocatedReservationSignature` is now
    `tuple[int, int, int, int, int, int, int]`.
- Complete: Add a regression test that fails before the implementation change.
  - Evidence: The targeted test failed before the worker implementation change
    with raw float tuple inequality.
- Complete: Run the narrow affected test after implementation.
  - Evidence: Targeted pytest command passed.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k allocated_reservation_signature
uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py
```

## Files Changed

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4495131102_ALLOCATED_SIGNATURE_PLAN.md`
- `plans/REVIEW_4495131102_ALLOCATED_SIGNATURE_VALIDATION.md`
