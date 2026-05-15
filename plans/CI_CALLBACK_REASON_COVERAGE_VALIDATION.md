# CI Callback Reason Catalog and Coverage Validation

## Plan Validation

- [x] `CALLBACK_TARGET_INVALID` is documented in `docs/REASON_CATALOG.md`.
- [x] `CALLBACK_TARGET_VALIDATION_TIMEOUT` is documented in `docs/REASON_CATALOG.md`.
- [x] Callback target validation and delivery helper branches have regression tests in `tests/unit/service/test_callbacks.py`.
- [x] The remaining full-suite coverage shortfall is covered by focused edge-case tests in existing unit-test areas.
- [x] No production code changed, and no CI check was disabled, skipped, or weakened.
- [x] The repository coverage gate passes locally at 99.01%.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py --cov=awf.service.callbacks --cov-report=term-missing -q`
  - Passed: `42 passed`; callback service coverage reached 99.17%.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py tests/unit/api/test_openapi_artifact.py tests/unit/docs/test_catalog_coverage.py -q`
  - Passed: `109 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/common/test_common_polish.py tests/unit/common/test_callback_targets.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/service/test_readiness.py tests/unit/service/test_failure_causality.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/control/test_worker_coverage_edges.py -q`
  - Passed during iterative validation of the added edge coverage.
- `uv run --python 3.12 --extra dev ruff check docs/REASON_CATALOG.md tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py tests/unit/common/test_callback_targets.py tests/unit/common/test_common_polish.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_worker_coverage_edges.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/service/test_callbacks.py tests/unit/service/test_failure_causality.py tests/unit/service/test_readiness.py`
  - Passed.
- `uv run --python 3.12 pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`
  - Passed: `6279 passed, 7 skipped`; total coverage 99.01%.

## Notes

`uv run --python 3.12 --extra dev mypy src/awf` was not rerun because this fix changes docs and tests only.
