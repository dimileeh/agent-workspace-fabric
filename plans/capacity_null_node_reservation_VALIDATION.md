# Capacity Null-Node Reservation Validation

Plan reference: `plans/capacity_null_node_reservation_PLAN.md`

## Requirement Status

- Add a regression test showing a null-node active workspace with a null-node
  active reservation consumes local capacity: Complete.
  Evidence: `tests/unit/control/test_worker.py` adds
  `test_requested_capacity_gate_counts_null_node_workspace_with_null_node_reservation`.
- Confirm the regression fails against the current implementation when
  practical: Complete.
  Evidence: the first focused test run failed before the implementation change
  because the capacity gate admitted the requested workspace instead of counting
  the null-node active workspace.
- Update allocation accounting so the mismatched-reservation branch treats
  `Workspace.node_id IS NULL` as local for the legacy null-reservation coverage
  path: Complete.
  Evidence: `src/awf/control/worker.py` now includes null-node workspaces in the
  candidate query and counts them only when the latest reservation is also
  node-unaware.
- Preserve behavior that remote-node active workspaces remain excluded:
  Complete.
  Evidence:
  `test_requested_capacity_gate_ignores_allocated_capacity_on_other_nodes`
  passed after the narrowing.
- Commit only the files changed for this review comment: Complete.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node_workspace_with_mismatched_reservation_node"`
  - Failed before implementation, as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node_workspace_with_null_node_reservation or mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes or ignores_unreserved_active_workspace_on_other_node or unreserved_active_local_workspace"`
  - Passed: 5 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

No remaining planned implementation gaps. The current database schema makes
`resource_reservations.node_id` non-null, so the test simulates the legacy
node-unaware reservation at the repository boundary while preserving existing
remote explicit reservation behavior.
