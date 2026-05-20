# PRRT_kwDOSJAM6s6DX8S Capacity Node Filter Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DX8S-` reports that local-capacity provisioning
scheduling sums allocated resource reservations across all nodes, then compares
those global totals against a single worker node's local capacity. Scope is the
capacity-gated requested-workspace claim path and the repository query needed to
support node-scoped totals.

## Requirements Checklist

- Add regression coverage proving a worker with local capacity configured ignores
  active reservations owned by other nodes when deciding whether to claim a
  requested workspace.
- Keep existing global reservation totals behavior available for metrics and
  workspace resource summaries.
- Update the worker capacity-gated claim path to compare local capacity against
  totals for the current worker node only.
- Preserve existing reservation status filtering and latest-active-per-workspace
  semantics.
- Commit the scoped fix locally with a conventional commit message for the review
  thread.

## Implementation Steps

1. Add a failing unit regression near the existing requested-capacity gate tests.
2. Add an optional `node_id` filter to `ResourceReservationRepository.active_latest_totals`.
3. Pass the worker node id, falling back to `local`, from the capacity-gated claim
   path.
4. Run the narrow regression, then the relevant worker/database unit surfaces.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<node-filter-regression> -q`
  passes after failing before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
  passes.
