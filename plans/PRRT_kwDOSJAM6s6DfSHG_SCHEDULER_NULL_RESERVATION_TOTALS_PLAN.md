# PRRT_kwDOSJAM6s6DfSHG Scheduler Null Reservation Totals Plan

## Problem Statement And Scope

The review thread reports that scheduler allocation totals exclude a legacy or
repair workspace when both `resource_reservations.node_id` and
`workspaces.node_id` are `NULL`. Because the capacity gate then sees an active
reservation row, the unreserved-workspace fallback does not add default demand,
which can undercount active local usage.

Scope is limited to the repository helper used by
`active_latest_totals_for_scheduler_allocation_scope` and focused regression
coverage for that helper.

## Requirements Checklist

- Add a regression showing scheduler allocation totals include a latest active
  reservation whose reservation node and workspace node are both `NULL`.
- Preserve existing behavior that a null-node workspace with an explicit
  non-local reservation remains excluded from a local node's scheduler totals.
- Keep the change scoped to scheduler allocation totals.
- Commit only files changed for this review thread.

## Implementation Steps

1. Add a focused repository regression in `tests/unit/db/test_scheduler_records.py`.
2. Confirm the regression fails against the current scheduler allocation filter
   when practical.
3. Update `_active_latest_resource_reservation_totals_stmt` so scheduler
   allocation scope includes rows where both node fields are `NULL`.
4. Run the focused regression and nearby repository scheduler totals test.
5. Write validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_scheduler_records.py` passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes if practical.
