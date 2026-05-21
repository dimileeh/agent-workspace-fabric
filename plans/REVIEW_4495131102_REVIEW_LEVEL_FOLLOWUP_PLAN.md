# Review 4495131102 Review-Level Follow-Up Plan

## Problem Statement and Scope

Review-level comment `issue:4495131102` reports three outside-diff observations
on the PR #270 local capacity scheduler work:

- `_requested_capacity_age_boost_changed` allegedly has a dead empty-window
  guard when `AGE_BOOST_MAX` is zero.
- `_capacity_queue_blocked_reason_counts` re-sorts candidates in Python after
  SQL has already applied scheduler ordering.
- `metrics_allocation_node_id` and `scheduler_allocation_node_id` deliberately
  count legacy null/null reservations in every node allocation scope, which can
  over-report per-node allocation in multi-node deployments unless the legacy
  compatibility scope is explicit.

Scope is limited to code comments and validation evidence in
`src/awf/service/metrics.py`, `src/awf/db/repositories.py`,
`tests/unit/control/test_worker.py`, `tests/unit/db/test_scheduler_records.py`,
and this plan/validation pair. No GitHub writes, pushes, branch changes, or
runtime behavior changes are planned.

## Requirements Checklist

- Preserve the existing age-boost guard because Python `range(1, 0 + 1)` is
  empty and an existing regression already covers `AGE_BOOST_MAX == 0`.
- Document why metrics keeps the bounded Python re-sort after SQL scheduler
  ordering.
- Document that the null/null allocation branch is deliberate single-node
  legacy compatibility and must be resolved before multi-node null-node rows
  can be treated as precise per-node allocation.
- Run focused tests covering the age-boost guard and allocation-scope legacy
  semantics.
- Run focused lint for touched Python files.

## Implementation Steps

1. Add a concise comment before the Python re-sort in
   `_capacity_queue_blocked_reason_counts`.
2. Add concise comments beside the scheduler and metrics allocation null/null
   predicates in `_active_latest_resource_reservation_totals_stmt`.
3. Leave `_requested_capacity_age_boost_changed` unchanged because the
   reviewer claim conflicts with Python `range` behavior and the existing
   `test_requested_capacity_age_boost_short_circuits_empty_windows`
   regression.
4. Run focused tests for age-boost and allocation-scope behavior.
5. Record validation evidence in
   `plans/REVIEW_4495131102_REVIEW_LEVEL_FOLLOWUP_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_age_boost_short_circuits_empty_windows"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "allocation_scope"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py src/awf/db/repositories.py tests/unit/control/test_worker.py tests/unit/db/test_scheduler_records.py`
  passes.
