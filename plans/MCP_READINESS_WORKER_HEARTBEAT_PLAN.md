# MCP Readiness Worker Heartbeat Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6HAgFD` reports that REST `GET /readyz` now includes
the worker heartbeat in `checks` and `overall_ok`, but the MCP fallback for
`awf_get_service_readiness` still omits the worker check. This can let MCP
clients see service readiness as healthy while REST readiness fails for a
missing or stale local worker heartbeat.

Scope is limited to the MCP fallback path and focused MCP readiness coverage.

## Requirements Checklist

- Add a regression test proving the MCP fallback reports a missing worker
  heartbeat as a failed `worker` check.
- Update `src/awf/mcp/server.py::_provided_readiness` fallback to run the same
  worker heartbeat check as REST readiness.
- Include the worker check in MCP fallback `overall_ok` calculation and payload.
- Keep injected readiness providers unchanged.
- Run focused tests only; full AWF/GitHub validation remains managed after agent
  completion.

## Implementation Steps

1. Add a focused MCP fallback regression test for missing worker heartbeat.
2. Confirm that test fails before implementation when practical.
3. Import and schedule `_check_worker_heartbeat` in the MCP fallback, using the
   resolved service node id.
4. Include `worker` in the fallback `checks` dictionary before docker checks.
5. Re-run the focused MCP readiness tests that cover the changed path.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py -q`
  passes.
- The pre-fix regression should fail with no `worker` check before the code
  change.
- Full repository validation is intentionally not run in-agent per AWF contract.
