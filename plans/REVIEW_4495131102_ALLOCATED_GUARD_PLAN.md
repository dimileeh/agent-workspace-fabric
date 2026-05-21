# Review 4495131102 Allocated Guard Plan

## Problem Statement and Scope

Review-level comment `issue:4495131102` reports that
`_capacity_queue_summary` calls `_scheduler_allocated_resources_for_session`
before checking whether any local capacity limits are configured. On
deployments without `local_capacity_cpu_cores`, `local_capacity_memory_gb`, or
`local_capacity_dind_slots`, that adds scheduler-allocation database queries to
every resource saturation request even though blocker counts must be empty.

Scope is limited to `src/awf/service/metrics.py`, focused service regression
coverage, and the plan/validation records required by the repository workflow.

## Requirements Checklist

- Skip scheduler-allocation resource queries when no local capacity constraints
  are explicitly configured.
- Preserve existing capacity queue totals: queued count, oldest queued
  workspace, wait age, and planned requested resources.
- Preserve blocker-count behavior when at least one local capacity constraint
  is configured.
- Add regression coverage that fails on the current unconditional allocation
  call and passes after the guard.
- Commit the fix locally without pushing, rebasing, or switching branches.

## Implementation Steps

1. Add a focused regression test in `tests/unit/service/test_metrics.py` that
   monkeypatches `_scheduler_allocated_resources_for_session` to fail and calls
   `_capacity_queue_summary` with no configured local capacity limits.
2. Confirm the focused regression fails before implementation.
3. Add a small helper or local guard in `src/awf/service/metrics.py` to detect
   configured local capacity constraints using the existing shared
   `LOCAL_CAPACITY_CONSTRAINTS` / `local_capacity_limit` contract.
4. Use an empty `ReservedResources` value for blocker simulation when no
   constraints are configured, avoiding the scheduler allocation query.
5. Run the focused regression and relevant metrics/static checks.
6. Record validation results in
   `plans/REVIEW_4495131102_ALLOCATED_GUARD_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_summary_skips_scheduler_allocation_when_unconstrained -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_summary or capacity_queue_blocked_reason_counts or resource_saturation_scopes_capacity_view_to_local_node or capacity_queue_uses_scheduler_allocation_scope"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/service/metrics.py`
  passes.
