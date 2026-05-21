# Review 4495131102 PostgreSQL Queue Signature Limit Plan

## Problem Statement And Scope

Review-level comment `issue:4495131102` flags that
`_requested_capacity_queue_signature` bounds the SQLite fallback scan but leaves
the PostgreSQL digest aggregation unbounded while the local-capacity scheduler
advisory lock is held. Scope is limited to the queue-signature helper, focused
worker unit coverage, and the required plan/validation records.

## Requirements Checklist

- Bound the PostgreSQL requested-capacity queue signature aggregation to the
  same representative frontier size used by the non-PostgreSQL path.
- Preserve existing signature fields: queue count within the sampled frontier,
  latest update time, latest created time, max workspace id, and digest.
- Keep the PostgreSQL `NULL` digest fallback behavior unchanged.
- Add a regression test proving the PostgreSQL query is capped before it reaches
  the aggregate digest.
- Do not switch branches, push, rebase, or touch unrelated files.

## Implementation Steps

1. Add failing unit coverage that compiles the PostgreSQL signature statement
   and asserts the capped frontier includes `LIMIT 500`.
2. Refactor the PostgreSQL query to aggregate over a limited, ordered subquery
   of requested workspaces for the scheduler node.
3. Keep the SQLite fallback path and explicit empty-string digest fallback
   behavior intact.
4. Run the focused new regression and related queue-signature tests.
5. Run lint for touched Python files and record validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passes.
