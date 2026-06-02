# Requested Node Scope Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GR3dl` reports that non-capacity requested provisioning claims are not node-scoped. When local capacity limits are disabled, a worker can list requested work without its node id and then claim a workspace that already has an active `ResourceReservation.node_id` for another worker node.

Scope is limited to requested provisioning scheduler selection and id-specific requested claims.

## Requirements Checklist

- Add a regression test proving a non-capacity worker on node B does not claim a requested workspace whose active reservation is for node A.
- Keep unreserved requested workspaces dispatchable for named non-capacity workers.
- Reuse existing scheduler node-scope semantics for requested rows.
- Guard the final id-specific requested transition so stale or incorrectly supplied ids cannot bypass node scope.
- Run only focused validation; broad AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add failing worker regression coverage for non-capacity requested reservation node scope.
2. Thread node id through non-capacity requested listing.
3. Add a transition guard for requested provisioning claims that matches the requested scheduler node-scope condition.
4. Run focused tests for the changed worker behavior.
5. Write validation evidence in `plans/REQUESTED_NODE_SCOPE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "non_capacity_requested"`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "list_requested_uses_non_capacity_limit_when_called_directly or non_capacity_requested"`
  - Passes for the changed focused surface.

Full AWF/GitHub validation is intentionally not run in this agent phase.
