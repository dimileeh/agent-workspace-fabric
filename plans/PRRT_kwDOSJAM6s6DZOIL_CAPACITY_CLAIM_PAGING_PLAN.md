# PRRT_kwDOSJAM6s6DZOIL Capacity Claim Paging Plan

## Problem Statement And Scope

When local capacity gating is enabled, requested workspace listing returns a
bounded scheduler candidate window before capacity checks run. If every
candidate in that window is capacity-blocked, later requested workspaces that
would fit can remain unclaimed on every worker poll.

Scope is limited to requested workspace provisioning claims with local capacity
gating in `src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Preserve existing scheduler ordering and provider suppression behavior for
  requested workspaces.
- Under the local capacity scheduler lock, continue paging through requested
  candidates until provision claim slots are filled or the requested queue is
  exhausted.
- Keep recording local capacity queue decisions for candidates deferred by
  capacity blockers.
- Do not change non-capacity provisioning claim behavior.
- Add a regression test where more head candidates are capacity-blocked than
  the requested candidate window and a later satisfiable candidate is claimed.

## Implementation Steps

1. Add a failing unit test for a requested queue with blocked head candidates
   longer than `_scheduler_candidate_fetch_limit(max_concurrent_provisions)`.
2. Update the local-capacity claim path to page requested scheduler candidates
   under the capacity lock and apply capacity checks across pages.
3. Reuse the existing scheduler filter for provider recovery and circuit-breaker
   suppression before capacity checks.
4. Verify the focused regression and nearby worker tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate"`
  must pass.
- If practical, run `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  and document the result.
