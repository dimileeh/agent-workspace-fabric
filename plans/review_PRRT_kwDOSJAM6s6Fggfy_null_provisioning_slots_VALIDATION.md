# Review PRRT_kwDOSJAM6s6Fggfy Null Provisioning Slots Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6Fggfy_null_provisioning_slots_PLAN.md`

## Requirement Status

- Complete: Regression test demonstrates a configured-node worker treats a
  `NULL` `node_id` provisioning row as occupying the last execution slot.
- Complete: `_requested_admission_row_slots()` preserves null-node worker
  isolation from named-node active rows.
- Complete: Focused validation passes after the fix.
- Complete: Broad validation is left to AWF/GitHub after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `tests/unit/control/test_worker_scheduler_admission.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_node_worker_counts_null_node_provisioning_rows_as_occupied -q`
  - Expected failing TDD check before implementation: failed with `assert 1 == 0`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_requested_workspace_stays_queued_when_node_active_rows_fill_slots tests/unit/control/test_worker_scheduler_admission.py::test_named_node_worker_counts_null_node_provisioning_rows_as_occupied tests/unit/control/test_worker_scheduler_admission.py::test_null_node_worker_admission_ignores_active_rows_on_named_nodes -q`
  - Passed: `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Passed: `6 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_scheduler_admission.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker/manager.py`
  - Passed.

Full AWF/GitHub-owned validation, coverage gates, and CI-equivalent checks were
not run in the agent phase per the workspace contract.
