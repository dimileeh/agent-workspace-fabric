# Setup Retry Counter Separation Validation

Plan reference: `plans/SETUP_RETRY_COUNTER_SEPARATION_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a setup dependency network retry does not
  consume the generic flaky retry budget.
- Complete: Split setup dependency retry count from generic flaky retry count in
  `ValidationRunner._run_commands`.
- Complete: Setup retry logging, backoff, attempt metadata, and exhaustion checks now
  use the setup-specific retry count.
- Complete: Final command retry counts report the combined retry total; setup recovery
  metadata reports the combined retry count and budget when both retry systems are used.
- Complete: Runtime validation unit tests, lint, and type checking pass.

## Evidence

Changed files:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/SETUP_RETRY_COUNTER_SEPARATION_PLAN.md`
- `plans/SETUP_RETRY_COUNTER_SEPARATION_VALIDATION.md`

Commands run:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget -q`
  failed before implementation because the command stopped with
  `VALIDATION_RETRY_EXHAUSTED` after the setup retry.
- Focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget -q`
  passed.
- Runtime validation module:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with `150 passed`.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/runtime/test_validation.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

No remaining gaps.
