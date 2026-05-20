# Capacity Review Comment 4495131102 Plan

## Problem Statement And Scope

Address the PR review-level findings for local-node FIFO capacity scheduling:

- capacity-gated requested scans must not evaluate workspaces routed to other nodes;
- resource saturation metrics should not duplicate latest-active reservation aggregation SQL;
- capacity queue-decision recording should surface requested workspaces that have no linked task attempt.

Scope is limited to scheduler candidate selection, repository-backed reservation aggregation for metrics, and diagnostic logging/tests for missing task attempts.

## Requirements Checklist

- Add regression coverage that fails when the capacity scheduler can see requested workspaces pinned to another node.
- Add regression coverage that metrics reservation totals are obtained through the repository-owned aggregation path while preserving workspace-node routing semantics.
- Add regression coverage that `_record_capacity_queue_decision` logs a warning when no `TaskAttempt` exists.
- Implement the smallest code changes needed to satisfy those tests.
- Preserve existing single-node and legacy `NULL` node behavior.
- Run focused unit tests covering scheduler, repository/metrics, and missing-attempt behavior.

## Implementation Steps

1. Extend `WorkspaceRepository.list_schedulable_workspaces` and the underlying scheduler statement with an optional workspace node-scope filter.
2. Pass the current worker node id into the capacity-gated requested scan only.
3. Add a repository method for latest active reservation totals scoped by workspace routing, backed by repository-owned SQL helpers.
4. Update metrics to delegate `_active_latest_totals_for_workspace_scope` to the repository method.
5. Log a warning from `_record_capacity_queue_decision` when the workspace has no linked task attempt.
6. Update affected tests and monkeypatch signatures.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<focused tests> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::<focused tests> tests/unit/db/test_scheduler_records.py::<focused tests> tests/unit/api/test_metrics_capacity.py::<focused tests> -q`
- Pass criteria: focused regression tests fail before implementation when practical, then pass after implementation.
