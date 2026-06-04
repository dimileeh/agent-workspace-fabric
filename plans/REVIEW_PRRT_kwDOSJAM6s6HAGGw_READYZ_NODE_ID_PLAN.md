# Review PRRT_kwDOSJAM6s6HAGGw Readyz Node ID Plan

## Problem Statement And Scope

The review thread reports that `/readyz` looks up worker heartbeats using the raw
resolved service node ID fallback expression, while the service worker records
heartbeats under the effective service node ID. A whitespace-only configured
worker node ID can therefore make readiness query a node that has no heartbeat.

Scope is limited to the `/readyz` worker heartbeat lookup and focused regression
coverage for whitespace-only configured worker node IDs.

## Requirements

- `/readyz` must use the same effective service node ID used by the service
  worker runtime.
- A whitespace-only configured worker node ID must fall back to the default
  local node for readiness heartbeat lookup.
- Keep the change minimal and avoid broad validation; AWF/GitHub owns full
  validation after the agent phase.

## Implementation Steps

1. Add a focused failing unit test for `/readyz` with a whitespace-only
   `worker_node_id` and a heartbeat recorded under the default local node.
2. Update `/readyz` to call the shared effective service node ID helper.
3. Run only the targeted test(s) that prove the regression and fix.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::<new test> -q`
  must fail before the implementation change and pass after it.
- Optionally run the adjacent worker heartbeat readiness tests in the same file
  if needed to cover nearby behavior.
- Do not run full repository tests, coverage gates, or full AWF validation in
  this agent phase.
