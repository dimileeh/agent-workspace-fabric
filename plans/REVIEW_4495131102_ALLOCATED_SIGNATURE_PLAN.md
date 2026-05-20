# Review 4495131102 Allocated Signature Plan

## Problem Statement And Scope

The capacity scheduler resume cursor compares `_AllocatedReservationSignature`
values for equality. The current signature stores raw float totals, so logically
identical allocated resource totals can differ when one path comes from Python
accumulation and another comes from SQL `SUM()` aggregation. This can
unnecessarily invalidate the resume cursor.

Scope is limited to the worker resume signature representation and a regression
test for float-drift normalization.

## Requirements Checklist

- [x] Preserve capacity scheduling correctness and existing cursor invalidation
      semantics for real allocation changes.
- [x] Stop raw float rounding drift from invalidating the resume cursor.
- [x] Keep the signature comparable with exact tuple equality.
- [x] Add a regression test that fails before the implementation change.
- [x] Run the narrow affected test after implementation.

## Implementation Steps

1. Add a unit test for logically equivalent allocation totals where Python float
   accumulation differs from an aggregate-style total.
2. Change `_AllocatedReservationSignature` to contain integer fields only.
3. Convert CPU and memory totals to fixed-point integer units in
   `_allocated_reservation_signature`.
4. Run the targeted worker test.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k allocated_reservation_signature
```

Pass criteria: the targeted regression test passes and no unrelated files are
modified.
