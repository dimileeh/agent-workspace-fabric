# Review 4495131102 Capacity Gate Roundtrip Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` calls out two remaining capacity-gate
cleanup items:

- `_capacity_deferred_decision_matches` should explicitly reject legacy
  resource summaries whose blocker signatures cannot be reconstructed.
- The advisory-locked capacity gate should not run a separate
  mismatched-node workspace-id query followed by `active_latest_by_workspace_ids`;
  it should use the repository-owned scheduler allocation totals query instead.

Scope is limited to worker capacity accounting, focused worker regressions, and
the review bookkeeping plan/validation artifacts.

## Requirements Checklist

- [ ] Preserve deferred-decision deduplication for current blocker summaries.
- [ ] Explicitly return `False` when a stored deferred capacity summary lacks a
  valid blocker signature.
- [ ] Make `_allocated_totals_for_capacity_gate` obtain persisted allocation
  totals through `active_latest_totals_for_scheduler_allocation_scope`.
- [ ] Preserve unreserved active workspace default accounting.
- [ ] Preserve existing local-node mismatch and null-node allocation behavior.
- [ ] Run focused worker tests and static checks for touched code.

## Implementation Steps

1. Add/update focused worker tests proving the capacity gate uses the scheduler
   allocation repository scope and still adds unreserved defaults.
2. Update `_capacity_deferred_decision_matches` with an explicit stored-signature
   guard.
3. Replace the worker's active-latest-plus-mismatched-reservation accounting
   path with the scheduler-allocation totals repository call.
4. Remove obsolete mismatch helper coverage or repurpose it toward the new
   capacity-gate behavior.
5. Run targeted pytest, ruff, mypy, and diff sanity checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_uses_scheduler_allocation_scope or unreserved_defaults_use_deduplicated_join or null_node_workspace_with_null_node_reservation or mismatched_reservation_node or skips_repeated_unchanged_capacity_deferral"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
- `git diff --check`
  passes.
