# Review 4495131102 Capacity SQL Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4495131102`:

- Remove the redundant `requested > limit` clause from
  `local_capacity_blocked_condition` while preserving blocker behavior.
- Replace correlated active-reservation `EXISTS` checks in the worker capacity
  accounting helpers with joins against a deduplicated active reservation
  workspace-id set.

Scope is limited to the shared capacity predicate, the worker helper queries,
and focused regression tests.

## Requirements Checklist

- [ ] Keep capacity queue blocked counts behaviorally equivalent for configured
  limits.
- [ ] Preserve unsatisfiable-vs-deferred classification in
  `local_capacity_blocker`.
- [ ] Ensure the SQL blocked-condition helper emits only
  `allocated + requested > limit`.
- [ ] Ensure worker active-reservation presence checks do not emit correlated
  `EXISTS` under the capacity gate.
- [ ] Preserve mismatched-node and unreserved-default allocation accounting.

## Implementation Steps

1. Add failing focused tests for the SQL predicate shape and worker query shape.
2. Simplify `local_capacity_blocked_condition` to the combined capacity
   comparison.
3. Add a small worker helper for the distinct active-reservation workspace-id
   subquery and use it with an inner join for reserved workspaces and a left
   outer join for unreserved workspaces.
4. Run the focused tests, then relevant ruff/mypy checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q -k local_capacity`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node or null_node_reservation or active_reservation_presence"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/resource_capacity.py src/awf/control/worker.py tests/unit/service/test_resource_capacity.py tests/unit/control/test_worker.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
