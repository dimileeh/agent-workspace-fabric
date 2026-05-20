# PRRT_kwDOSJAM6s6DbNkU Capacity Baseline Plan

## Problem Statement and Scope

The requested capacity gate builds its local allocated baseline from active resource reservations filtered by reservation `node_id`. During provisioning handoff, a workspace can already be assigned to the local worker through `Workspace.node_id` while its active reservation still has the create-time node. The reservation is then omitted from local totals, and the unreserved-workspace fallback skips it because an active reservation exists.

Scope is limited to the worker capacity gate accounting path and a regression test for the review thread.

## Requirements Checklist

- Count active latest reservations for allocated-status workspaces assigned to the local worker even when the reservation `node_id` differs.
- Preserve existing behavior that reservations for other workers do not consume this worker's capacity.
- Preserve the unreserved active workspace fallback.
- Add a regression test that fails before the fix and passes after it.
- Run the narrow unit test surface that proves the change.

## Implementation Steps

1. Add a unit regression in `tests/unit/control/test_worker.py` for a local active workspace whose active reservation has a mismatched node.
2. Confirm the new test fails before implementation when practical.
3. Update `src/awf/control/worker.py` so `_allocated_totals_for_capacity_gate` supplements repository node totals with latest active reservations whose workspace is assigned to the local node but whose reservation node was not counted.
4. Re-run the targeted capacity gate tests.
5. Create `plans/PRRT_kwDOSJAM6s6DbNkU_CAPACITY_BASELINE_VALIDATION.md` with requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate"`
  - Passes with the new regression included.
- If time permits, run `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`.
  - Passes without new lint failures.
