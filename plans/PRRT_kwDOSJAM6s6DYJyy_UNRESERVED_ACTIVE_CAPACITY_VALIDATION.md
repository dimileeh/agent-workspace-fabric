# PRRT_kwDOSJAM6s6DYJyy Unreserved Active Capacity Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DYJyy_UNRESERVED_ACTIVE_CAPACITY_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving an active local workspace without
  an active reservation contributes default CPU, memory, and DinD demand to the
  capacity baseline.
- Complete: Preserved latest active reservation accounting by keeping persisted
  reservation totals as the first allocation source.
- Complete: Preserved node scoping by counting local and legacy null-node
  unreserved active workspaces while ignoring unreserved active rows owned by a
  different node.
- Complete: Requested candidates remain outside the allocated baseline because
  the fallback only scans `ALLOCATED_RESOURCE_RESERVATION_STATUSES`.
- Complete: Scoped changes are ready for a local conventional commit for the
  review thread.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DYJyy_UNRESERVED_ACTIVE_CAPACITY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DYJyy_UNRESERVED_ACTIVE_CAPACITY_VALIDATION.md`

Commands run:

- Pre-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_defers_for_unreserved_active_local_workspace -q`
  failed with `assert 1 == 0`, proving the regression.
- Post-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_defers_for_unreserved_active_local_workspace -q`
  passed.
- Post-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_ignores_unreserved_active_workspace_on_other_node -q`
  passed.
- Post-fix: `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- Post-fix: `uv run --python 3.12 --extra dev mypy src/awf` passed.
- Post-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed: 195 tests.
