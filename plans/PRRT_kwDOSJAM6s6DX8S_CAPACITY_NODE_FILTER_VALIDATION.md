# PRRT_kwDOSJAM6s6DX8S Capacity Node Filter Validation

Plan reference: `PRRT_kwDOSJAM6s6DX8S_CAPACITY_NODE_FILTER_PLAN.md`

## Requirement Status

- Complete: Regression coverage proves a worker with local capacity configured
  ignores active reservations owned by other nodes. Evidence:
  `tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_ignores_allocated_capacity_on_other_nodes`
  failed before implementation with `assert 0 == 1`, then passed after the fix.
- Complete: Existing global reservation totals behavior remains available.
  Evidence: `ResourceReservationRepository.active_latest_totals()` keeps
  `node_id=None` as the default and
  `tests/unit/db/test_scheduler_records.py::test_resource_reservation_active_latest_totals_can_filter_by_node_id`
  asserts global totals include both nodes.
- Complete: Worker capacity-gated claims compare local capacity against current
  worker node totals only. Evidence: `src/awf/control/worker.py` passes
  `node_id=self._config.node_id or "local"` to `active_latest_totals()`.
- Complete: Existing status filtering and latest-active-per-workspace semantics
  are preserved. Evidence: the repository regression filters to
  `WorkspaceStatus.provisioning`, excludes a requested reservation, and counts
  only the latest active reservation for a workspace.
- Complete: Scoped fix is ready to commit locally.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_ignores_allocated_capacity_on_other_nodes -q`
  - Failed before implementation as expected.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py::test_resource_reservation_active_latest_totals_can_filter_by_node_id -q`
  - Passed after adding the missing test import.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passed: 191 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
  - Passed: 7 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py tests/unit/control/test_worker.py tests/unit/db/test_scheduler_records.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
