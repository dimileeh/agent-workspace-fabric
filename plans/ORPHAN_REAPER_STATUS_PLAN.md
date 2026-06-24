# Orphan Reaper Status Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6L9c7t` reports that `awf service status`
marks orphan resources healthy when `auto_cleanup_orphans=true` even if no
live worker is available to reap them. The fix is limited to the CLI status
orphan-resource path.

## Requirements Checklist

- Verify worker/reaper liveness before turning an orphan failure into an
  `ORPHANS_PRESENT_REAPING_ENABLED` success in `collect_service_status`.
- Keep orphan checks failing when auto-cleanup is enabled but the worker
  heartbeat is missing, stale, or unavailable.
- Preserve the existing auto-cleanup success state when the worker heartbeat is
  fresh.
- Keep the change scoped to status behavior and focused tests.

## Implementation Steps

1. Add a focused regression test for auto-cleanup with orphan resources and no
   worker heartbeat.
2. Add a status-side worker heartbeat probe using the existing worker heartbeat
   freshness semantics.
3. Gate both `orphan_workspaces` and `orphan_resources` reaping success on that
   probe.
4. Update existing auto-cleanup status tests to model a fresh worker heartbeat.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py -q`
  - Passes, proving the focused status behavior.

Full AWF/GitHub validation is owned by AWF after this agent completes and is
not run inside this workspace phase.
