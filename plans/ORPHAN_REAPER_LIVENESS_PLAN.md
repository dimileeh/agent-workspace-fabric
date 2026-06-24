# Orphan Reaper Liveness Plan

## Problem Statement and Scope

The orphan resource summary can currently report orphan resources as `ok` and
cleanup-ready when `auto_cleanup_orphans` is true, even if the caller has not
proven that a worker reaper is alive. `/readyz` and MCP readiness already pass
worker heartbeat state through the health helper, but the shared summary builder
and the metrics saturation default path still allow liveness-dependent promotion
without an explicit liveness proof.

Scope is limited to the orphan resource summary promotion gate and focused
regression coverage for the review thread `PRRT_kwDOSJAM6s6L-Fm4`.

## Requirements Checklist

- Require explicit reaper liveness proof before `build_orphan_resource_summary`
  promotes orphan resources under `auto_cleanup_orphans`.
- Preserve existing reaping-enabled behavior for callers that have proven a
  live reaper, including readiness paths and the worker reaper itself.
- Ensure the metrics saturation default summary does not advertise automatic
  reaping without liveness proof.
- Add focused regression tests for the shared builder and metrics default path.
- Run only targeted tests relevant to the changed behavior; full AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add an explicit liveness argument to `build_orphan_resource_summary` and use
   it in the reaping promotion decision.
2. Update proven-live callers to pass the explicit liveness argument.
3. Update tests that intentionally model a live worker/reaper.
4. Add or adjust tests proving unproven metrics/default summary remains blocked.
5. Run focused unit tests for orphan resources, health readiness, MCP readiness,
   and metrics saturation behavior touched by the change.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused test selections> -q`
  passes for the changed behavior.
- No broad suite, coverage gate, frontend build, push, or branch operation is
  run during the agent phase.
