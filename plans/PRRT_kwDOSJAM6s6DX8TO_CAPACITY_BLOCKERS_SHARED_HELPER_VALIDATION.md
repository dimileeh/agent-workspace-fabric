# PRRT_kwDOSJAM6s6DX8TO Capacity Blockers Shared Helper Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DX8TO_CAPACITY_BLOCKERS_SHARED_HELPER_PLAN.md`

## Requirement Status

- Complete: worker capacity admission behavior is preserved by routing
  `_local_capacity_blockers` through shared `LocalCapacityBlocker` helpers while
  keeping the existing payload fields and unsatisfiable classification.
- Complete: metrics capacity queue aggregation still builds SQL aggregate
  expressions and the existing SQL-shape regression remains green.
- Complete: the duplicated capacity dimension/reason-code list now lives in
  `LOCAL_CAPACITY_CONSTRAINTS` in `src/awf/service/resource_capacity.py`.
- Complete: focused helper regression coverage was added in
  `tests/unit/service/test_resource_capacity.py`.
- Complete: focused resource-capacity, worker, metrics, lint, and type checks
  passed.

## Evidence

Files changed:

- `src/awf/service/resource_capacity.py`
- `src/awf/control/worker.py`
- `src/awf/service/metrics.py`
- `tests/unit/service/test_resource_capacity.py`
- `plans/PRRT_kwDOSJAM6s6DX8TO_CAPACITY_BLOCKERS_SHARED_HELPER_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DX8TO_CAPACITY_BLOCKERS_SHARED_HELPER_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q`
  - Result: failed before implementation because the shared helper API did not
    exist.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q`
  - Result: passed, `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/service/test_metrics.py -q`
  - Result: passed, `272 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/resource_capacity.py src/awf/control/worker.py src/awf/service/metrics.py tests/unit/service/test_resource_capacity.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/resource_capacity.py src/awf/control/worker.py src/awf/service/metrics.py`
  - Result: passed.

## Gaps

No planned gaps remain.
