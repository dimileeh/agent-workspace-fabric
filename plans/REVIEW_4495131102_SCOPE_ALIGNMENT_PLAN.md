# Review 4495131102 Scope Alignment Plan

## Problem Statement and Scope

Address PR review comment `issue:4495131102` follow-up findings for local-node
capacity scheduling metrics and reservation lookup style.

The reviewer identified two issues:

- `capacity_queue.blocked_reason_counts` seeds its FIFO simulation from the
  metrics allocation scope, which can undercount scheduler-gated capacity when
  a reservation belongs to the local node but the workspace row still points to
  another node during migration.
- `ResourceReservationRepository.active_latest_by_workspace_ids` selects all
  active rows and deduplicates in Python instead of using the repository's
  established `ROW_NUMBER() OVER (PARTITION BY workspace_id ...)` pattern.

Scope is limited to metrics scheduler-scope allocation for queue blocker
simulation, the resource reservation repository lookup, focused regression
tests, and this plan/validation pair. No GitHub writes, pushes, branch changes,
or unrelated refactors.

## Requirements Checklist

- Add a regression test proving capacity queue blocker counts use scheduler
  allocation scope when a local-node reservation belongs to a workspace whose
  `workspace.node_id` points elsewhere.
- Keep the public allocated resource metrics behavior unchanged unless a test
  proves it must change.
- Add or update a regression test proving
  `active_latest_by_workspace_ids` returns only the latest active reservation
  per workspace.
- Ensure the reservation batch lookup uses the SQL `ROW_NUMBER()` window
  pattern rather than Python-side deduplication.
- Run focused tests for the changed metrics and repository behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_metrics.py` and
   `tests/unit/db/test_scheduler_records.py` for the two review findings.
2. Run the focused new tests and confirm they fail before implementation.
3. Add a scheduler-allocation helper in `src/awf/service/metrics.py` and use
   it only as the seed for capacity queue blocker simulation.
4. Rewrite `ResourceReservationRepository.active_latest_by_workspace_ids` to
   join against a ranked subquery filtered to rank 1.
5. Run focused tests and static checks for the touched files.
6. Record requirement-by-requirement validation in
   `plans/REVIEW_4495131102_SCOPE_ALIGNMENT_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "scheduler_allocation_scope_for_migrating_reservation"`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "active_latest_by_workspace_ids_uses_window_query"`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py src/awf/db/repositories.py tests/unit/service/test_metrics.py tests/unit/db/test_scheduler_records.py`
  passes.
