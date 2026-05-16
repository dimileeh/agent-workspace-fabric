# PRRT_kwDOSJAM6s6B_JjC Plan

## Problem Statement And Scope

The unresolved PR review thread reports that primary failure causality selects the
latest failed validation run by `finished_at`, `started_at`, then random
`ValidationRun.id`. Because validation run IDs are uuid4-derived, equal
timestamps can attach arbitrary reason-code and coverage evidence to
`primary_failure`.

Scope is limited to deterministic failure-causality validation provenance
selection and a regression test for the reported tie.

## Requirements Checklist

- Add a regression test where two failed validation runs for one workspace have
  identical `finished_at` and `started_at`, but the lexicographically larger ID
  belongs to the older run.
- Select the chronologically later validation run without using random IDs as
  the timestamp tie-breaker.
- Preserve current filtering of ignored stale callback validation runs.
- Keep the change scoped to failure-causality behavior.

## Implementation Steps

1. Add the failing regression test in `tests/unit/service/test_failure_causality.py`.
2. Replace the UUID fallback in `_latest_failed_validation_run` with persisted
   chronological metadata from `ValidationRun`.
3. Run the targeted failure-causality test file.
4. Run narrow lint/type checks if time permits after the targeted test passes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if runtime permits.
