# PRRT_kwDOSJAM6s6DbNkU Capacity Baseline Validation

## Plan Reference

`plans/PRRT_kwDOSJAM6s6DbNkU_CAPACITY_BASELINE_PLAN.md`

## Requirement Status

- Count active latest reservations for allocated-status workspaces assigned to the local worker even when the reservation `node_id` differs: Complete.
  - Evidence: `src/awf/control/worker.py` now supplements reservation-node totals with latest active reservations for workspaces whose `Workspace.node_id` is local but whose reservation node differs.
- Preserve existing behavior that reservations for other workers do not consume this worker's capacity: Complete.
  - Evidence: `test_requested_capacity_gate_ignores_allocated_capacity_on_other_nodes` remains green.
- Preserve the unreserved active workspace fallback: Complete.
  - Evidence: the existing fallback remains unchanged and still runs after the mismatched-reservation supplement.
- Add a regression test that fails before the fix and passes after it: Complete.
  - Evidence: `test_requested_capacity_gate_counts_local_workspace_with_mismatched_reservation_node` failed before implementation with `assert 1 == 0`, then passed after implementation.
- Run the narrow unit test surface that proves the change: Complete.
  - Evidence: targeted capacity-gate pytest and touched-file ruff checks passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node"`
  - Pre-fix result: failed as expected with the worker claiming one workspace.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes"`
  - Post-fix result: passed, 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate"`
  - Result: passed, 14 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Remaining Gaps

None.
