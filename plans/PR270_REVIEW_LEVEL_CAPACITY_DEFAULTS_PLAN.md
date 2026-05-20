# PR270 Review-Level Capacity Defaults Plan

## Problem Statement And Scope

Address review-level feedback from PR #270 comment `4326189459` for capacity accounting and queue-decision ordering. The scope is limited to:

- `ResourceReservationRepository.active_latest_totals()` node filtering.
- Metrics capacity summaries for missing reservation rows with DinD profiles.
- Worker queue-decision persistence for defaulted reservations after successful claims.

## Requirements Checklist

- Verify and fix `active_latest_totals(node_id=...)` so `node_id` filters already-ranked latest active reservations per workspace.
- Preserve global and status-filtered latest reservation behavior.
- Mirror worker default DinD demand in resource metrics when active/requested workspaces have no active reservation row.
- Ensure allocated, planned, and blocked queue metrics include default DinD slots from `Workspace.resolved_profile`.
- Record `LOCAL_CAPACITY_RESERVATION_DEFAULTED` queue decisions only after `requested -> provisioning` succeeds.
- Add focused regression tests before implementation.
- Keep changes minimal and do not alter unrelated scheduler or metrics behavior.

## Implementation Steps

1. Add failing regression coverage for node-scoped latest reservation totals when a workspace's newest active reservation belongs to another node.
2. Add failing metrics coverage for unreserved DinD-profile workspaces in active, allocated, planned, and blocked queue summaries.
3. Add failing worker coverage proving stale claim races do not persist defaulted ordered queue decisions.
4. Update repository query shape to include `node_id` in the ranked subquery and apply the node filter after `reservation_rank == 1`.
5. Add metrics helpers that derive fallback DinD demand from `Workspace.resolved_profile`, including SQL expression support for blocked reason aggregation.
6. Move defaulted ordered queue decision recording below the successful `transition_if_current()` check.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py tests/unit/service/test_metrics.py tests/unit/control/test_worker.py -q`
  - Passes targeted regression and adjacent coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - No lint regressions.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - No type regressions in source.
