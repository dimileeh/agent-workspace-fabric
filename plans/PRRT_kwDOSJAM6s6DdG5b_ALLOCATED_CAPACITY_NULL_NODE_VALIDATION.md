# PRRT_kwDOSJAM6s6DdG5b Allocated Capacity Null Node Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DdG5b_ALLOCATED_CAPACITY_NULL_NODE_PLAN.md`

## Requirement Status

- Complete: Added a regression showing a null-node active workspace with a
  non-local latest reservation is excluded from local `allocated_resources`.
  Evidence: `tests/unit/service/test_metrics.py`.
- Complete: The same regression verifies the excluded allocation does not
  create local capacity queue blockers.
  Evidence: `summary.capacity_queue.blocked_reason_counts == {}`.
- Complete: The regression preserves counting for a local workspace whose
  latest reservation still names a prior node.
  Evidence: `summary.allocated_resources.peak_cpu == 2.0`.
- Complete: Reserved/planned workspace-scoped totals remain unchanged.
  Evidence: regression asserts reserved active count and planned requested
  count still include the workspace-routed rows.
- Complete: Changes are scoped to this thread.
  Evidence: changed files are `src/awf/service/metrics.py`,
  `tests/unit/service/test_metrics.py`, and this thread's plan/validation docs.

## Verification Evidence

- Confirmed failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_resource_saturation_allocated_capacity_matches_scheduler_null_node_rules -q`
  failed with `allocated_resources.active_workspace_count == 2`.
- Passed focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_resource_saturation_allocated_capacity_matches_scheduler_null_node_rules -q`.
- Passed full metrics module:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  (`86 passed`).
- Passed lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`.
- Passed type check:
  `uv run --python 3.12 --extra dev mypy src/awf`.

## Gaps

No known gaps remain.
