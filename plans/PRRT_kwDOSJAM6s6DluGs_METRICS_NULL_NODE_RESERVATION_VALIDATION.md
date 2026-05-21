# Metrics Null-Node Reservation Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DluGs_METRICS_NULL_NODE_RESERVATION_PLAN.md`

## Requirement Status

- Add a regression test for metrics allocation totals that includes a legacy
  null-node reservation on a null-node workspace: Complete.
  Evidence:
  `test_resource_reservation_metrics_allocation_scope_counts_null_node_reservation`
  was added to `tests/unit/db/test_scheduler_records.py`.
- Confirm the new test fails against the current metrics predicate when
  practical: Complete.
  Evidence: the first focused run failed because metrics allocation returned
  zero totals for the null/null reservation.
- Update `active_latest_totals_for_metrics_allocation_scope` so null/null active
  reservations are included consistently with scheduler allocation: Complete.
  Evidence: the metrics allocation predicate in
  `src/awf/db/repositories.py` now includes rows where both reservation and
  workspace node IDs are `NULL`.
- Preserve exclusion of null-workspace reservations explicitly assigned to a
  different node: Complete.
  Evidence: the regression inserts a second null-workspace reservation assigned
  to `node-b`; the expected totals include only the null/null row.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "metrics_allocation_scope_counts_null_node_reservation"`
  - Failed before implementation, as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "allocation_scope"`
  - Passed: 3 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_scheduler_records.py`
  - Passed.

## Gaps

No remaining planned implementation gaps.
