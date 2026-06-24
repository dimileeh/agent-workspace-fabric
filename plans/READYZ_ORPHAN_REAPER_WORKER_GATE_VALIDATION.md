# Readyz Orphan Reaper Worker Gate Validation

Plan reference: `plans/READYZ_ORPHAN_REAPER_WORKER_GATE_PLAN.md`

## Requirement Status

- Gate the readyz orphan-resource reaping-enabled upgrade on a healthy worker
  heartbeat: Complete.
- Preserve existing behavior when no orphan resources are present, scanners/DB
  are unavailable, or auto cleanup is disabled: Complete.
- Keep the MCP readiness fallback aligned with `/readyz`: Complete.
- Add focused regression coverage for auto-cleanup enabled with a missing worker
  heartbeat: Complete.
- Run only targeted tests for the changed readiness behavior: Complete.

## Evidence

Changed files:

- `src/awf/api/routes/health.py`
- `src/awf/mcp/server.py`
- `tests/unit/api/test_health_parts/test_health_part_002.py`

Focused validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py -q`
  - Passed: `12 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py::TestMcpOperatorSurfaceParityPart001::test_readiness_fallback_propagates_auto_cleanup_orphans -q`
  - Passed: `1 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/health.py src/awf/mcp/server.py tests/unit/api/test_health_parts/test_health_part_002.py`
  - Passed

Full AWF/GitHub validation, full coverage, and CI-equivalent suites were not
run in this agent phase per the AWF workspace contract; AWF owns those after
agent completion.

## Gaps

None.
