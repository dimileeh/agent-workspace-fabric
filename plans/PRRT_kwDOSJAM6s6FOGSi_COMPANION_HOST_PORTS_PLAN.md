# PRRT_kwDOSJAM6s6FOGSi Companion Host Ports Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6FOGSi` reports that companion port validation accepts repeated host ports. Docker Compose later renders each companion mapping as `host_port:container_port`, so repeated host ports cause bind failures after workspace setup has already started.

Scope is limited to rejecting duplicate companion host ports before stack launch and preserving existing companion validation behavior.

## Requirements Checklist

- Reject duplicate host ports within one `WorkspaceCompanionRequest`.
- Reject duplicate host ports across multiple companion requests in one `WorkspaceCreateRequest`.
- Reject duplicate host ports during companion/profile graph validation before companion worktrees are materialized, including profile service collisions.
- Add regression tests for the duplicate-host-port cases.
- Run focused tests only; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add request-schema regression tests for duplicate host ports within one companion and across companions.
2. Add graph-validation regression tests for duplicate host ports across companion/profile services.
3. Update companion request validation to track repeated host ports in each request.
4. Update workspace create companion normalization to track repeated companion host ports across the request.
5. Update companion/profile graph validation to reject duplicate host ports before dependency/cycle checks.
6. Run targeted tests for the changed schema and companion graph behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_duplicate_host_ports -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_validate_companion_service_graph_rejects_duplicate_host_ports -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py -q`
  - Focused files pass after implementation.

Full repository validation, coverage gates, OpenAPI drift checks, and CI-equivalent commands are intentionally not run in the agent phase per the AWF workspace contract.
