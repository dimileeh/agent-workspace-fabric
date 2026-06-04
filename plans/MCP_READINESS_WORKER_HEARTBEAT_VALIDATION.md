# MCP Readiness Worker Heartbeat Validation

Plan reference: `MCP_READINESS_WORKER_HEARTBEAT_PLAN.md`

## Requirement Status

- Add a regression test proving the MCP fallback reports a missing worker
  heartbeat as a failed `worker` check: Complete.
- Update `src/awf/mcp/server.py::_provided_readiness` fallback to run the same
  worker heartbeat check as REST readiness: Complete.
- Include the worker check in MCP fallback `overall_ok` calculation and payload:
  Complete.
- Keep injected readiness providers unchanged: Complete.
- Run focused tests only; full AWF/GitHub validation remains managed after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py`
- `tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py`
- `plans/MCP_READINESS_WORKER_HEARTBEAT_PLAN.md`
- `plans/MCP_READINESS_WORKER_HEARTBEAT_VALIDATION.md`

Focused checks:

- Pre-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py::TestMcpOperatorSurfaceParityPart001::test_readiness_fallback_reports_missing_worker_heartbeat -q`
  failed with `KeyError: 'worker'`.
- Post-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py::TestMcpOperatorSurfaceParityPart001::test_readiness_fallback_reports_missing_worker_heartbeat -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py -q`
  passed: 16 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_service_readiness_tool_matches_rest_payload -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py`
  passed.

Full AWF/GitHub validation was not run in-agent per workspace contract.

## Gaps

None.
