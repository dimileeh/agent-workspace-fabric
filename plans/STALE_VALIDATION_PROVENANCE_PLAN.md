# Stale Validation Provenance Plan

## Problem Statement And Scope

An unresolved review thread reports that `load_primary_failure_snapshot()` can
attach a later stale validation callback run as primary validation provenance.
When that happens, `STALE_CALLBACK_IGNORED` can replace the original validation
failure reason and coverage evidence in preserved cleanup or runtime-failure
payloads.

Scope is limited to failure-causality provenance selection and regression
coverage for stale validation callback runs.

## Requirements Checklist

- Reproduce the stale callback overwrite with a focused unit regression.
- Keep the original validation failure run and coverage as primary evidence.
- Exclude stale callback validation runs from automatic primary provenance
  attachment.
- Do not weaken existing embedded-primary or secondary-failure behavior.

## Implementation Steps

1. Add a regression in `tests/unit/service/test_failure_causality.py` that seeds
   a primary `PYTEST_TEST_FAILURE`, then records a later failed validation run
   with reason code `STALE_CALLBACK_IGNORED`.
2. Confirm the new regression fails before the production fix.
3. Update `src/awf/service/failure_causality.py` so stale callback validation
   runs are not considered for primary validation provenance.
4. Run the focused failure-causality test module.
5. Run the narrow lint/type checks if practical for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  should pass or any unrelated pre-existing failure must be documented.
