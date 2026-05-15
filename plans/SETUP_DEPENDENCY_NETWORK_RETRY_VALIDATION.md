# Setup Dependency Network Retry Validation

Plan reference: `plans/SETUP_DEPENDENCY_NETWORK_RETRY_PLAN.md`

## Requirement Status

- Complete: transient dependency/index setup failures classify as
  `SETUP_DEPENDENCY_NETWORK_FAILURE` with package, host, category,
  retryability, retry counts, and bounded redacted diagnostics.
- Complete: setup-only transient dependency failures retry with bounded budget
  and backoff; retry success preserves the original failure evidence in the
  same command artifacts and metadata.
- Complete: exhausted transient setup retries fail before agent execution with
  precise setup dependency/network reason metadata while retaining the existing
  coarse `service_startup_failure` taxonomy.
- Complete: deterministic setup failures are not retried and keep existing
  generic command/setup failure behavior.
- Complete: executor emits structured retry and retry-exhausted workspace events
  and redacts event/terminal payload diagnostics.
- Complete: implementation scope stayed limited to validation setup retry
  handling, executor observability, and focused tests.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `src/awf/control/executor.py`
- `tests/unit/runtime/test_validation.py`
- `tests/unit/control/test_executor_error_paths.py`
- `plans/SETUP_DEPENDENCY_NETWORK_RETRY_PLAN.md`
- `plans/SETUP_DEPENDENCY_NETWORK_RETRY_VALIDATION.md`

Validation commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestValidationInfrastructureError::test_validation_runner_exception_finishes_validation_run -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges::test_recheck_after_setup_stops_when_workspace_was_cancelled -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_error_paths.py tests/unit/control/test_executor_validation_fix_cycle.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Results:

- Runtime validation tests: `140 passed`
- Focused executor regression checks: both targeted legacy-stub regressions passed
- Focused regression surface: `255 passed`
- Ruff: passed
- Mypy: passed for `154 source files`

## Notes

The observed `docker==7.1.0` locked dependency was not changed. The bug was the
setup failure handling: a transient DNS/PyPI fetch error now gets classified,
retried, and reported as setup dependency/network infrastructure rather than an
opaque provider or generic service startup failure.

## Iteration 1

Conformance gaps addressed:

- Complete: added explicit regression coverage for non-DNS transient dependency
  network categories: connection reset, connection refused, connect timeout,
  read timeout, TLS handshake timeout, and dependency index HTTP 5xx.
- Complete: runtime setup dependency retry and retry-exhausted log entries now
  use the classifier's redacted, bounded command string instead of the raw
  setup command, preventing command-embedded package index credentials from
  leaking.
- Complete: added a regression with command-embedded credentials and an
  over-limit setup command to prove retry log command fields are redacted and
  truncated.

Additional files changed in iteration 1:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/SETUP_DEPENDENCY_NETWORK_RETRY_VALIDATION.md`

Iteration 1 validation commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_covers_transient_shapes tests/unit/runtime/test_validation.py::test_setup_dependency_retry_logs_redact_and_truncate_command_credentials -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_error_paths.py tests/unit/control/test_executor_validation_fix_cycle.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Iteration 1 results:

- New focused regressions: `7 passed`
- Focused saved-plan surface: `262 passed`
- Ruff: passed
- Mypy: passed for `154 source files`
