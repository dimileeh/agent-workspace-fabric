# Coverage And Parallel DB Stability Plan

## Summary

Bring the current maintainability-clean branch back above the 99% coverage gate and
remove the local `-n 20` Postgres/Alembic flakiness observed in the full coverage
run. This is a test/stability pass only: do not change AWF product behavior.

## Current Evidence

- Full coverage command failed at `98.15%`, below `--cov-fail-under=99`.
- The three failed/erroring nodes passed when rerun directly, pointing at
  parallel live-Postgres contention rather than deterministic product failure.
- Largest coverage misses are extracted helper modules from the decomposition:
  `runtime/validation_coverage.py`, `cli/common.py`, `cli/init_ops.py`,
  `runtime/validation_setup.py`, `control/executor/execution_flow.py`, and
  `control/worker/cleanup.py`.

## Implementation Steps

1. Add focused unit coverage for extracted helper branches instead of excluding
   code from coverage.
2. Stabilize the Alembic/live-Postgres test helper so subprocess migration
   sequences cannot interleave with other schema DDL during xdist runs.
3. Add regression coverage for the stability fix where practical without making
   tests depend on real timing races.
4. Rerun focused tests, then the full `-n 20` coverage gate.

## Validation

```bash
uv run --python 3.12 --extra dev pytest -n 20 --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
```

The branch is not PR-ready until that command passes cleanly.
