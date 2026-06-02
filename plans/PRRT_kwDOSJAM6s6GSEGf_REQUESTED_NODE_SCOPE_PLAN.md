# PRRT_kwDOSJAM6s6GSEGf Requested Node Scope Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GSEGf` reports that requested workspaces with
`Workspace.node_id IS NULL` and an active legacy `ResourceReservation.node_id`
set to an old local container hostname are no longer visible to current local
workers. Current local service workers use the stable node id `local`, so those
legacy requested rows can remain stuck.

Scope is limited to requested-workspace scheduler listing and claim scoping.
Named multi-node workers must continue to ignore requested work reserved for
another named node.

## Requirements Checklist

- Preserve requested scheduler node scoping for named workers.
- Allow stable `local` workers to see and claim legacy requested rows whose
  workspace node is null but active reservation node contains an older non-local
  hostname.
- Keep unreserved requested rows visible to the appropriate scheduler scope.
- Add regression tests for the local legacy fallback and named-node isolation.
- Run only focused tests for the changed scheduler behavior; full AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a requested scheduler node-scope fallback for `node_id == "local"` that
   admits null-workspace-node rows with any active reservation node.
2. Keep the existing `COALESCE(Workspace.node_id, latest_active_reservation.node_id)`
   behavior for non-local named workers.
3. Add repository-level regression tests covering local legacy reservation
   visibility and named-node rejection.
4. Add one worker-path regression proving `_list_requested()` and direct
   non-capacity claims can adopt the legacy local reservation row.
5. Run the targeted repository and worker test nodes that exercise the changed
   condition.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py -q`
  should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "non_capacity_local_requested_claim_adopts_legacy_reservation_hostname or non_capacity_requested_claim_honors_reservation_node"`
  should pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/_scheduler.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/control/test_worker_parts/test_worker_part_003.py`
  should pass.
- Do not run broad coverage, full unit suite, or full CI-equivalent validation
  during this agent phase.
