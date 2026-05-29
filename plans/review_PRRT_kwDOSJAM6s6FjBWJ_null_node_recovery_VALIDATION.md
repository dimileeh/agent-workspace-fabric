# Validation: PRRT_kwDOSJAM6s6FjBWJ Null-Node Recovery

## Plan Check

- Added a regression proving a named worker scans a legacy `NULL`-node
  provisioning row that can consume requested-admission capacity.
- Updated stale-active recovery candidate selection to include the same
  legacy `NULL`-node scope that requested admission counts.
- Kept validation focused per the AWF workspace contract; full AWF/GitHub
  validation is managed after agent completion.

## Evidence

- Failed before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q`
  - Result: failed because `inspector.calls == []`.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q`
  - Result: `1 passed`.
- Related focused regression set:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_node_worker_counts_null_node_provisioning_rows_as_occupied tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission tests/unit/control/test_worker_scheduler_admission.py::test_null_node_worker_admission_ignores_active_rows_on_named_nodes tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_admission_waits_for_null_node_lock_before_claiming tests/unit/control/test_worker_scheduler_admission.py::test_healthy_ready_workspace_waiting_for_slot_is_not_stale_execution -q`
  - Result: `5 passed`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/admission.py src/awf/control/worker/recovery_stale.py tests/unit/control/test_worker_scheduler_admission.py`
  - Result: passed.
- Whitespace:
  `git diff --check -- src/awf/control/worker/admission.py src/awf/control/worker/recovery_stale.py tests/unit/control/test_worker_scheduler_admission.py plans/review_PRRT_kwDOSJAM6s6FjBWJ_null_node_recovery_PLAN.md`
  - Result: passed.
