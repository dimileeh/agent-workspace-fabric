# PRRT_kwDOSJAM6s6GQqGx Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GQqGx_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `WorkspaceService.create` rejects a host-port conflict on the configured worker node.
- Complete: Kept unset worker node identity on the existing local single-node default by replacing the worker hostname fallback with the shared `"local"` resolver.
- Complete: Ensured explicit `worker_node_id` values drive both host-port conflict scans and reservation records through `effective_worker_node_id`.
- Complete: Ran focused validation only; full AWF/GitHub validation remains owned by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/service/node_identity.py`
- `src/awf/service/worker.py`
- `src/awf/service/workspaces.py`
- `src/awf/service/workspaces_create.py`
- `src/awf/api/routes/workspaces.py`
- `src/awf/service/pr_monitor_adoption.py`
- `src/awf/service/metrics_capacity.py`
- `tests/unit/service/test_worker.py`
- `tests/unit/service/test_scheduler_records.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local -q`
  - Failed before implementation with provisioner node id equal to the container hostname instead of `local`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local tests/unit/service/test_worker.py::test_build_worker_runtime_uses_local_service_node_id_instead_of_container_hostname tests/unit/service/test_scheduler_records.py::test_create_rejects_host_port_conflict_on_configured_worker_node tests/unit/service/test_scheduler_records.py::test_create_writes_admitted_decision_and_local_reservation tests/unit/service/test_host_port_conflict_helper.py -q`
  - Passed: 41 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/node_identity.py src/awf/service/worker.py src/awf/service/workspaces.py src/awf/service/workspaces_create.py src/awf/api/routes/workspaces.py src/awf/service/pr_monitor_adoption.py src/awf/service/metrics_capacity.py tests/unit/service/test_worker.py tests/unit/service/test_scheduler_records.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py tests/unit/service/test_scheduler_records.py tests/unit/service/test_host_port_conflict_helper.py -q`
  - Passed: 61 tests.

## Remaining Gaps

None for the planned scope. Full AWF/GitHub validation, provenance capture, and merge gating are intentionally left to AWF after this agent phase.
