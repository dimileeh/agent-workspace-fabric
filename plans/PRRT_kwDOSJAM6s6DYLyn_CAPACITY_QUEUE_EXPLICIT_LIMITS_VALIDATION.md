# PRRT_kwDOSJAM6s6DYLyn Capacity Queue Explicit Limits Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DYLyn_CAPACITY_QUEUE_EXPLICIT_LIMITS_PLAN.md`

## Requirement Status

- Add regression coverage proving queue blocked-reason counts do not report CPU
  or memory saturation from detected local capacity when explicit CPU and memory
  limits are unset: Complete.
- Preserve explicit CPU, memory, and DIND blocked-reason behavior: Complete.
- Keep queue blocker counts aligned with scheduler enforcement in
  `src/awf/control/worker.py`: Complete.
- Commit the scoped fix locally with a conventional commit message for the
  review thread: Complete after local commit.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`
- `plans/PRRT_kwDOSJAM6s6DYLyn_CAPACITY_QUEUE_EXPLICIT_LIMITS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DYLyn_CAPACITY_QUEUE_EXPLICIT_LIMITS_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_ignores_detected_cpu_and_memory_limits -q`
  failed before implementation with extra CPU and memory blocker counts, then
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  passed, 82 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  passed, 11 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  passed.

## Gaps

None.
