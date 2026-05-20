# PRRT_kwDOSJAM6s6DYLyn Capacity Queue Explicit Limits Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DYLyn` reports that capacity queue
`blocked_reason_counts` use auto-detected CPU and memory capacity as fallback
limits even though scheduler dispatch blockers only enforce explicitly
configured local CPU and memory limits. Scope is the requested-workspace
blocked-reason count path in `src/awf/service/metrics.py`.

## Requirements Checklist

- Add regression coverage proving queue blocked-reason counts do not report CPU
  or memory saturation from detected local capacity when explicit CPU and memory
  limits are unset.
- Preserve explicit CPU, memory, and DIND blocked-reason behavior.
- Keep queue blocker counts aligned with scheduler enforcement in
  `src/awf/control/worker.py`.
- Commit the scoped fix locally with a conventional commit message for the
  review thread.

## Implementation Steps

1. Add a focused unit regression around `_capacity_queue_blocked_reason_counts`.
2. Confirm the regression fails against the current implementation.
3. Remove detected CPU and memory fallback from queue blocked-reason counts so
   only explicit local capacity settings contribute blockers.
4. Run the narrow regression and relevant metrics test surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_ignores_detected_cpu_and_memory_limits -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  passes.
