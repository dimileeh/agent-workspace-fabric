# PRRT_kwDOSJAM6s6DX8TO Capacity Blockers Shared Helper Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DX8TO` reports duplicated local capacity
blocking reason logic between `src/awf/control/worker.py` and
`src/awf/service/metrics.py`. The scope is to extract the shared resource
dimension/reason-code mapping and blocker predicate into the existing local
capacity utility module without changing scheduling or metrics behavior.

## Requirements Checklist

- [ ] Keep worker capacity admission behavior unchanged, including
      unsatisfiable vs deferred blocker classification.
- [ ] Keep metrics capacity queue aggregation unchanged, including SQL-only
      aggregation of requested rows.
- [ ] Remove the duplicated capacity dimension/reason-code list from worker
      and metrics in favor of one shared utility source.
- [ ] Add focused regression coverage for the shared helper behavior.
- [ ] Validate the focused worker, metrics, and resource-capacity tests.

## Implementation Steps

1. Add focused failing tests in `tests/unit/service/test_resource_capacity.py`
   for shared local capacity constraints, limits, and blocker classification.
2. Add shared constraint metadata and blocker predicate/helper functions to
   `src/awf/service/resource_capacity.py`.
3. Update `src/awf/control/worker.py` to compute blockers from the shared
   helper while preserving the existing queue-decision payload shape.
4. Update `src/awf/service/metrics.py` to build capacity blocked reason SQL
   expressions from the same shared constraints and predicate.
5. Run focused tests for resource capacity, worker capacity admission, and
   metrics capacity aggregation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/service/test_metrics.py -q`

Pass criteria: all commands exit successfully, and the metrics SQL aggregation
test still proves a single aggregate query.
