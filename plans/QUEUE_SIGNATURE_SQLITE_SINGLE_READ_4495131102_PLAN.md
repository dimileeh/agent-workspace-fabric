# Queue Signature SQLite Single Read 4495131102 Plan

## Problem Statement and Scope

Address PR review comment `issue:4495131102` noting that the non-PostgreSQL
fallback in `_requested_capacity_queue_signature` reads requested queue
aggregate fields and digest fields with two separate awaits. A SQLite test path
mutation between those reads can produce a signature that never represented one
database snapshot. Production PostgreSQL already computes the signature in a
single statement.

Scope is limited to the non-PostgreSQL fallback and focused worker regression
coverage. No branch changes, pushes, GitHub comments, or unrelated refactors.

## Requirements Checklist

- Add a regression test proving the non-PostgreSQL queue signature path performs
  one `execute` call for a single queue snapshot.
- Keep the signature tuple shape unchanged.
- Preserve existing signature contents: count, latest `updated_at`, latest
  `created_at`, max workspace id, and digest over queue-ordering fields.
- Keep the PostgreSQL signature path unchanged.

## Implementation Steps

1. Add a failing test in `tests/unit/control/test_worker.py` with a fake SQLite
   session that raises if `_requested_capacity_queue_signature` performs more
   than one `execute`.
2. Confirm the focused test fails against the current split-query fallback.
3. Update the non-PostgreSQL branch in `src/awf/control/worker.py` to select
   requested queue rows once and compute the aggregate signature fields in
   Python from that one result set.
4. Run the focused queue-signature tests and static checks for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"`
  passes after the fix, with the new regression failing before implementation
  when practical.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
