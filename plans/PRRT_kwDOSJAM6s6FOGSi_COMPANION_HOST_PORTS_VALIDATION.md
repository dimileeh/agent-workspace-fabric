# PRRT_kwDOSJAM6s6FOGSi Companion Host Ports Validation

Plan reference: `PRRT_kwDOSJAM6s6FOGSi_COMPANION_HOST_PORTS_PLAN.md`

## Requirement Status

- Complete: Reject duplicate host ports within one `WorkspaceCompanionRequest`.
- Complete: Reject duplicate host ports across multiple companion requests in one `WorkspaceCreateRequest`.
- Complete: Reject duplicate host ports during companion/profile graph validation before companion worktrees are materialized, including profile service collisions.
- Complete: Add regression tests for the duplicate-host-port cases.
- Complete: Run focused tests only; full AWF/GitHub validation remains managed after agent completion.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `src/awf/api/schemas.py`
- `src/awf/node/companion_services.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `tests/unit/node/test_companion_services.py`

Focused TDD failure before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_duplicate_host_ports -q`
  - Failed with 2 cases that did not raise `ValidationError`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_validate_companion_service_graph_rejects_duplicate_host_ports -q`
  - Failed with 3 cases that did not raise `ProfileResolutionError`.

Focused passing checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_duplicate_host_ports -q`
  - `2 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_validate_companion_service_graph_rejects_duplicate_host_ports -q`
  - `3 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py -q`
  - `54 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py src/awf/api/schemas_companions.py src/awf/node/companion_services.py tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py`
  - `All checks passed!`

Full repository validation, coverage gates, OpenAPI drift checks, and CI-equivalent commands were not run in the agent phase per the AWF workspace contract.
