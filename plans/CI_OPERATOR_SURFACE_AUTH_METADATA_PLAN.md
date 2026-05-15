# CI Operator Surface Auth Metadata Plan

## Problem Statement and Scope

PR #250 fails the Python full coverage CI job because operator-surface contract
tests are stale relative to the implemented auth hardening. The focused failure
set shows:

- the contract registry still expects `/release-readiness` to use a generic
  `dict[str, object]` response model even though the route now declares
  `ReleaseReadinessResponse`;
- MCP/REST parity tests call newly protected operator metadata REST endpoints
  without the fixture bearer token.

This plan is limited to restoring contract/test alignment without weakening the
auth check or broadening the implementation.

## Requirements Checklist

- [ ] Keep protected operator metadata endpoints protected; do not remove
  `require_api_token` or relax auth tests.
- [ ] Update the capability registry so `/release-readiness` metadata matches
  the actual FastAPI response model.
- [ ] Update MCP parity REST calls for protected endpoints to use the existing
  test fixture auth headers.
- [ ] Run the focused failing pytest node IDs and confirm they pass.
- [ ] Run the relevant broader contract/MCP surface tests needed for confidence.

## Implementation Steps

1. Change the `core_release_readiness` registry entry from
   `dict[str, object]` to `ReleaseReadinessResponse`.
2. Add `headers=operator_stack.auth_headers` or
   `headers=resource_stack.auth_headers` to the failing parity REST calls.
3. Re-run the exact CI failure nodes.
4. Re-run the affected contract and MCP parity test modules.

## Verification Commands and Pass Criteria

Focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py::test_rest_route_metadata_matches_registry[core_release_readiness] tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_core_release_readiness_tool_matches_rest_payload tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_merge_queue_tool_matches_rest_payload_and_reason_codes tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_failure_analysis_metrics_tool_matches_rest_payload tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_workspace_reliability_and_slo_tools_match_rest_payloads tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_resource_saturation_tool_matches_rest_payload_with_fake_providers tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_overlap_graph_tool_matches_rest_payload -q
```

Broader confidence:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/mcp/test_mcp_operator_surfaces.py -q
```

Pass criteria: all listed tests pass and the route metadata test still asserts
auth dependency parity for protected operator metadata surfaces.
