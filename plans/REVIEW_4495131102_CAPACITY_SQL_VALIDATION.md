# Review 4495131102 Capacity SQL Validation

Plan reference: `plans/REVIEW_4495131102_CAPACITY_SQL_PLAN.md`

## Requirement Status

- Complete: Keep capacity queue blocked counts behaviorally equivalent for
  configured limits.
  - Evidence: `tests/unit/service/test_metrics.py -q -k
    capacity_queue_blocked_reason_counts` passed.
- Complete: Preserve unsatisfiable-vs-deferred classification in
  `local_capacity_blocker`.
  - Evidence: `tests/unit/service/test_resource_capacity.py -q -k
    local_capacity` passed.
- Complete: Ensure the SQL blocked-condition helper emits only
  `allocated + requested > limit`.
  - Evidence:
    `test_local_capacity_blocked_condition_uses_combined_capacity_predicate`
    failed before implementation on the redundant `requested > limit OR ...`
    SQL and passed after implementation.
- Complete: Ensure worker active-reservation presence checks do not emit
  correlated `EXISTS` under the capacity gate.
  - Evidence:
    `test_capacity_gate_active_reservation_presence_uses_deduplicated_joins`
    failed before implementation on correlated `EXISTS` SQL and passed after
    implementation with distinct workspace-id joins.
- Complete: Preserve mismatched-node and unreserved-default allocation
  accounting.
  - Evidence: the focused worker slice covering mismatched-node, null-node, and
    active-reservation presence scenarios passed.

## Commands Run

- Expected failing pre-implementation checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q -k local_capacity`
  - Result before implementation: failed on the redundant SQL predicate shape.
- Expected failing pre-implementation checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "active_reservation_presence"`
  - Result before implementation: failed on correlated `EXISTS` SQL.
- Focused shared capacity check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q -k local_capacity`
  - Result after implementation: passed, `5 passed, 2 deselected`.
- Focused worker allocation check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node or null_node_reservation or active_reservation_presence"`
  - Result after implementation: passed, `3 passed, 204 deselected`.
- Metrics aggregate check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k capacity_queue_blocked_reason_counts`
  - Result: passed, `3 passed, 83 deselected`.
- Static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/resource_capacity.py src/awf/control/worker.py tests/unit/service/test_resource_capacity.py tests/unit/control/test_worker.py`
  - Result: passed.
- Type checks:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- Whitespace sanity:
  `git diff --check`
  - Result: passed.

## Remaining Gaps

None.
