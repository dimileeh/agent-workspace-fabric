# Readyz Orphan Reaper Worker Gate Plan

## Problem Statement And Scope

The `/readyz` orphan resource check can report `ORPHANS_PRESENT_REAPING_ENABLED`
when `auto_cleanup_orphans` is enabled even if the same readiness payload reports
the worker heartbeat check as failed. That overstates automatic reaping because
the worker is the component that performs the reap.

Scope is limited to the readiness health path and its MCP fallback mirror. The
generic orphan summary builder remains unchanged for non-readyz callers.

## Requirements Checklist

- Gate the readyz orphan-resource reaping-enabled upgrade on a healthy worker
  heartbeat.
- Preserve existing behavior when no orphan resources are present, scanners/DB
  are unavailable, or auto cleanup is disabled.
- Keep the MCP readiness fallback aligned with `/readyz`.
- Add focused regression coverage for auto-cleanup enabled with a missing worker
  heartbeat.
- Run only targeted tests for the changed readiness behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add an optional worker check dependency to the health orphan-resource helper.
2. Pass the worker heartbeat task/result from `/readyz` and the MCP fallback.
3. When worker heartbeat is not OK, do not pass auto-cleanup as enabled to the
   orphan summary, so orphan resources remain blocked/report-only.
4. Add or update focused tests in the readyz health tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py -q`
  must pass.
- If the MCP fallback call-site change needs direct evidence, run the narrow MCP
  readiness fallback test that covers auto-cleanup propagation.
- Full repository validation, coverage gates, and CI-equivalent commands are not
  run during this agent phase; AWF owns those after completion.
