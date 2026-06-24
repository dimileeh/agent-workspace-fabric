# Orphan Reaper Liveness Validation

Plan reference: `plans/ORPHAN_REAPER_LIVENESS_PLAN.md`

## Requirement Status

- Complete: Require explicit reaper liveness proof before
  `build_orphan_resource_summary` promotes orphan resources under
  `auto_cleanup_orphans`.
  - Evidence: `src/awf/service/orphan_resources.py` gates reaping promotion on
    both `auto_cleanup_orphans` and `reaper_available`.
- Complete: Preserve existing reaping-enabled behavior for proven-live callers.
  - Evidence: `src/awf/api/routes/health.py` passes worker heartbeat state into
    the summary builder, and the worker sweep path passes `reaper_available=True`
    because it is executing inside the reaper.
- Complete: Ensure metrics saturation default does not advertise automatic
  reaping without liveness proof.
  - Evidence: the default metrics summary does not pass `reaper_available`, and
    the regression test asserts blocked/dry-run behavior.
- Complete: Add focused regression tests.
  - Evidence: added shared-builder regression for missing liveness and updated
    metrics regression for unproven liveness.
- Complete: Run focused validation only.
  - Evidence: commands below passed. Full AWF/GitHub validation remains managed
    by AWF after agent completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py::test_orphan_summary_reports_reaping_enabled_as_ok tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py::test_orphan_summary_auto_cleanup_without_reaper_liveness_stays_blocked tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/api/test_metrics_capacity.py::test_default_orphan_resource_summary_blocks_auto_cleanup_without_reaper_liveness tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_reflect_auto_cleanup_enabled tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_auto_cleanup_orphans_requires_worker_heartbeat tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py -k readiness_fallback_propagates_auto_cleanup_orphans -q`
  - Result: passed, but the `-k` filter selected only the MCP readiness test.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py::test_orphan_summary_reports_reaping_enabled_as_ok tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py::test_orphan_summary_auto_cleanup_without_reaper_liveness_stays_blocked tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/api/test_metrics_capacity.py::test_default_orphan_resource_summary_blocks_auto_cleanup_without_reaper_liveness tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_reflect_auto_cleanup_enabled tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_auto_cleanup_orphans_requires_worker_heartbeat -q`
  - Result: passed, `32 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/orphan_resources.py src/awf/api/routes/health.py src/awf/api/routes/metrics.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_health_parts/test_health_part_002.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py`
  - Result: passed.

## Remaining Gaps

None for this plan. Broad validation and coverage gates were intentionally not
run during the agent phase per the AWF workspace contract.
