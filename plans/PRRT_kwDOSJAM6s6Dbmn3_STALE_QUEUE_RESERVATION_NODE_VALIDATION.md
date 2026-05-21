# PRRT_kwDOSJAM6s6Dbmn3 Stale Queue Reservation Node Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Dbmn3_STALE_QUEUE_RESERVATION_NODE_PLAN.md`

## Requirement Status

- Add a regression test for a requested workspace routed to the local node with
  a latest active reservation on a different `node_id`: Complete.
- Confirm the regression fails before implementation when practical: Complete.
- Make blocked-reason counts use latest active reservation demand regardless of
  stale reservation `node_id`: Complete.
- Preserve SQL aggregation behavior and workspace node-scope filtering:
  Complete.
- Validate with targeted unit tests: Complete.

## Evidence

Changed files:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`

Validation commands:

- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -k test_capacity_queue_blocked_reason_counts_uses_stale_node_reservation_demand -q`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -k "capacity_queue_blocked_reason_counts" -q`
- Passed lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`

No remaining gaps.
