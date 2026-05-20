# Review 4495131102 Capacity Gate Roundtrip Validation

Plan reference: `plans/REVIEW_4495131102_CAPACITY_GATE_ROUNDTRIP_PLAN.md`

## Requirement Status

- Complete: Preserve deferred-decision deduplication for current blocker
  summaries.
  - Evidence: `test_requested_capacity_gate_skips_repeated_unchanged_capacity_deferral`
    remains green after the explicit stored-signature guard.
- Complete: Explicitly return `False` when a stored deferred capacity summary
  lacks a valid blocker signature.
  - Evidence: `_capacity_deferred_decision_matches` now stores the reconstructed
    signature first and returns `False` when it is `None`.
- Complete: Make `_allocated_totals_for_capacity_gate` obtain persisted
  allocation totals through `active_latest_totals_for_scheduler_allocation_scope`.
  - Evidence: `test_capacity_gate_uses_scheduler_allocation_scope_and_unreserved_defaults`
    fails if the worker calls `active_latest_totals` or
    `active_latest_by_workspace_ids`.
- Complete: Preserve unreserved active workspace default accounting.
  - Evidence: the scheduler-scope capacity-gate test verifies defaults are added
    on top of persisted allocation totals; the unreserved-default join test
    remains green.
- Complete: Preserve existing local-node mismatch and null-node allocation
  behavior.
  - Evidence: focused worker tests covering mismatched reservation nodes and
    null-node remote reservations passed.
- Complete: Run focused worker tests and static checks for touched code.
  - Evidence: commands below.

## Verification Evidence

- Expected failing pre-implementation check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_uses_scheduler_allocation_scope"`
  - Result before implementation: failed because `_allocated_totals_for_capacity_gate`
    called `active_latest_totals`.
- Focused worker checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_uses_scheduler_allocation_scope or unreserved_defaults_use_deduplicated_join or null_node_workspace_with_null_node_reservation or mismatched_reservation_node or skips_repeated_unchanged_capacity_deferral"`
  - Result: passed, `5 passed, 205 deselected`.
- Repository scheduler-scope check:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  - Result: passed, `1 passed, 9 deselected`.
- Broader worker capacity slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k capacity`
  - Result: passed, `23 passed, 187 deselected`.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- Diff sanity:
  `git diff --check`
  - Result: passed.

## Gaps

No known gaps remain.
