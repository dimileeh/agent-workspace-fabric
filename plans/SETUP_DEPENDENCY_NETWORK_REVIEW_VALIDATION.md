# Setup Dependency Network Review Validation

Plan reference: `plans/SETUP_DEPENDENCY_NETWORK_REVIEW_PLAN.md`

## Requirement Status

- Complete: Preserve the precise `SETUP_DEPENDENCY_NETWORK_FAILURE` reason on
  recovery operation rows when setup dependency retry exhaustion is the root
  cause.
- Complete: Keep generic setup recovery failures using
  `MONITOR_RECOVERY_SETUP_FAILED`.
- Complete: Tighten HTTP 5xx classification so context-free `status code 5xx`
  output is not classified as a dependency index failure, while real HTTP/index
  5xx cases remain covered by existing tests.
- Complete: Existing regression tests were strengthened; no tests were deleted
  or weakened.
- Complete: Work was committed locally on the current AWF-managed branch after
  validation.

## Evidence

Files changed:

- `src/awf/control/executor.py`
- `src/awf/runtime/validation.py`
- `tests/unit/control/test_executor_monitor_recovery.py`
- `tests/unit/runtime/test_validation.py`
- `plans/SETUP_DEPENDENCY_NETWORK_REVIEW_PLAN.md`
- `plans/SETUP_DEPENDENCY_NETWORK_REVIEW_VALIDATION.md`

TDD failure confirmed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_non_http_status_code_5xx tests/unit/control/test_executor_monitor_recovery.py::test_setup_dependency_exhaustion_during_recovery_preserves_precise_monitor_reason -q`
  failed on the expected classifier false positive and generic recovery
  operation reason.

Passing verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_non_http_status_code_5xx tests/unit/control/test_executor_monitor_recovery.py::test_setup_dependency_exhaustion_during_recovery_preserves_precise_monitor_reason tests/unit/control/test_executor_monitor_recovery.py::test_generic_setup_failure_during_recovery_preserves_monitor_setup_reason -q`
  passed with `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_monitor_recovery.py -q`
  passed with `264 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py src/awf/runtime/validation.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_monitor_recovery.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
