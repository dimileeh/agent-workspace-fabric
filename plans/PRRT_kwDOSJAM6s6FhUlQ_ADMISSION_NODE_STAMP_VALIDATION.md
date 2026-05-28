# PRRT_kwDOSJAM6s6FhUlQ Admission Node Stamp Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FhUlQ_ADMISSION_NODE_STAMP_PLAN.md`

## Requirement Status

- Complete: Add regression coverage proving named workers stamp `Workspace.node_id`
  when claiming requested rows into `provisioning`.
  - Evidence: `tests/unit/control/test_worker_scheduler_admission.py` adds direct
    and local-capacity claim regressions that assert `node_id == "local"` after
    `run_once()` claims the workspace.
- Complete: Cover both requested claim paths: direct claim and local-capacity claim.
  - Evidence: `test_named_worker_stamps_node_id_when_claiming_requested_for_provisioning`
    covers `_claim_requested_for_provisioning`; `test_named_local_capacity_worker_stamps_node_id_when_claiming_requested`
    covers `_claim_requested_capacity_candidates`.
- Complete: Preserve existing admission behavior that counts legacy NULL-node
  active rows for named workers.
  - Evidence: `tests/unit/control/test_worker_scheduler_admission.py` still passes,
    including `test_named_node_worker_counts_null_node_provisioning_rows_as_occupied`.
- Complete: Keep the ownership stamp in the same DB transaction as the requested claim.
  - Evidence: `src/awf/control/worker/claims.py` assigns `ws.node_id` after the
    guarded requested-to-provisioning transition and before the transaction commit
    in both claim paths.
- Complete: Do not run broad AWF/GitHub validation.
  - Evidence: Only focused pytest nodes, the focused admission test file, and
    narrow ruff checks on touched Python files were run. Full validation remains
    managed by AWF/GitHub after agent completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_stamps_node_id_when_claiming_requested_for_provisioning tests/unit/control/test_worker_scheduler_admission.py::test_named_local_capacity_worker_stamps_node_id_when_claiming_requested -q`
  - Before implementation: failed with `Workspace.node_id` still `None`.
  - After implementation: passed, 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Passed, 12 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py tests/unit/control/test_worker_scheduler_admission.py`
  - Passed.

## Remaining Gaps

None.
