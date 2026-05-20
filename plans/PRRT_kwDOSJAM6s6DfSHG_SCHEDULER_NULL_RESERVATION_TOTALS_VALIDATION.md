# PRRT_kwDOSJAM6s6DfSHG Scheduler Null Reservation Totals Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DfSHG_SCHEDULER_NULL_RESERVATION_TOTALS_PLAN.md`

## Requirement Status

- Complete: Added a regression showing scheduler allocation totals include a
  latest active reservation whose reservation node and workspace node are both
  `NULL`.
  Evidence: `tests/unit/db/test_scheduler_records.py` adds
  `test_resource_reservation_scheduler_allocation_scope_counts_null_node_reservation`.
- Complete: Preserved behavior that a null-node workspace with an explicit
  non-local reservation remains excluded from a local node's scheduler totals.
  Evidence: the same regression seeds a `node-b` reservation for a null-node
  workspace and expects only the null/null reservation totals.
- Complete: Kept the implementation scoped to scheduler allocation totals.
  Evidence: `src/awf/db/repositories.py` changes only the
  `scheduler_allocation_node_id` predicate in
  `_active_latest_resource_reservation_totals_stmt`.
- Complete: Files changed for this thread are limited to the repository, the
  focused repository test, and this thread's plan/validation docs.

## Verification Evidence

- Confirmed failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py::test_resource_reservation_scheduler_allocation_scope_counts_null_node_reservation -q`
  failed because all allocated totals were zero.
- Passed focused scheduler allocation tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  (`2 passed, 9 deselected`).
- Passed full scheduler-records repository test module:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
  (`11 passed`).
- Passed lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_scheduler_records.py`.
- Passed type check:
  `uv run --python 3.12 --extra dev mypy src/awf`.

## Gaps

No known gaps remain.
