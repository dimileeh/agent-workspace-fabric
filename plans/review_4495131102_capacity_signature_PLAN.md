# Review 4495131102 Capacity Signature Plan

## Problem Statement And Scope

Address the review-level feedback on PR #270 for the local capacity scheduler
queue signature helper. Scope is limited to `src/awf/control/worker.py`, focused
unit regressions, and the required plan/validation records.

## Requirements Checklist

- Bound the non-PostgreSQL requested-capacity queue signature scan so SQLite
  development/test backends do not read an unbounded requested queue inside the
  scheduler transaction.
- Preserve the current single-snapshot SQLite fallback behavior.
- Make the PostgreSQL aggregate digest fallback explicit when the driver returns
  `NULL`, returning an empty digest string instead of the literal `"None"`.
- Add regression tests for both review observations.
- Keep changes scoped and avoid branch changes, pushes, rebases, or unrelated
  refactors.

## Implementation Steps

1. Add failing unit coverage for the SQLite fallback statement having a bounded
   `LIMIT`.
2. Add failing unit coverage for a PostgreSQL aggregate row with `ids_digest` as
   `None`.
3. Add a small local constant for the SQLite signature frontier limit and apply
   it to the fallback select.
4. Change the PostgreSQL digest string coercion to use an explicit empty-string
   fallback.
5. Run focused worker unit tests and, if practical, lint the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
