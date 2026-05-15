# CI Operator Surface Auth Metadata Validation

Plan reference: `plans/CI_OPERATOR_SURFACE_AUTH_METADATA_PLAN.md`

## Requirement Status

- Complete: Protected operator metadata endpoints remain protected. No
  production route dependencies were changed, and the surface metadata test
  continues to assert `require_api_token` parity for protected routes.
- Complete: The `core_release_readiness` registry entry now expects
  `ReleaseReadinessResponse`, matching the FastAPI route metadata.
- Complete: MCP parity REST calls for the protected release readiness, merge
  queue, metrics, resource saturation, SLO, and overlap graph endpoints now use
  the existing bearer-token fixture headers.
- Complete: The exact CI failure node IDs pass locally.
- Complete: The affected contract and MCP parity modules pass locally.

## Evidence

Files changed:

- `tests/unit/contracts/_capabilities.py`
- `tests/unit/mcp/test_mcp_operator_surfaces.py`
- `plans/CI_OPERATOR_SURFACE_AUTH_METADATA_PLAN.md`
- `plans/CI_OPERATOR_SURFACE_AUTH_METADATA_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py::test_rest_route_metadata_matches_registry[core_release_readiness] tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_core_release_readiness_tool_matches_rest_payload tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_merge_queue_tool_matches_rest_payload_and_reason_codes tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_failure_analysis_metrics_tool_matches_rest_payload tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_workspace_reliability_and_slo_tools_match_rest_payloads tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_resource_saturation_tool_matches_rest_payload_with_fake_providers tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_overlap_graph_tool_matches_rest_payload -q
```

Result: `7 passed in 5.97s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/mcp/test_mcp_operator_surfaces.py -q
```

Result: `179 passed in 35.64s`

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/contracts/_capabilities.py tests/unit/mcp/test_mcp_operator_surfaces.py
```

Result: `All checks passed!`

## Remaining Gaps

None for the planned CI failure fix.
