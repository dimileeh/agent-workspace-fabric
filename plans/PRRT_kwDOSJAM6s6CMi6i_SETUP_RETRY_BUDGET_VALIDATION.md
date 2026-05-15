# PRRT_kwDOSJAM6s6CMi6i Setup Retry Budget Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CMi6i_SETUP_RETRY_BUDGET_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving setup dependency retry exhaustion
  reports the setup dependency retry budget when the profile also configures
  generic flaky validation retries.
- Complete: Final `ValidationCommandResult.retry_count` remains the combined
  command retry count.
- Complete: Setup dependency attempt lineage, recovery metadata, and retry
  exhausted reason behavior remain unchanged.
- Complete: Updated the mixed setup/flaky retry test so
  `setup_dependency_network.retry_budget` is scoped to the setup dependency
  retry budget.
- Complete: Focused runtime validation tests, runtime validation module, ruff,
  and mypy passed.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_kwDOSJAM6s6CMi6i_SETUP_RETRY_BUDGET_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CMi6i_SETUP_RETRY_BUDGET_VALIDATION.md`

Regression-first check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget tests/unit/runtime/test_validation.py::test_setup_dependency_network_exhaustion_reports_setup_retry_budget_only -q
```

Result before implementation: failed because the metadata reported combined
budgets (`3` and `4`) instead of setup dependency retry budgets (`2` and `1`).

Final verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget tests/unit/runtime/test_validation.py::test_setup_dependency_network_exhaustion_reports_setup_retry_budget_only -q
```

Result: `2 passed in 0.90s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Result: `176 passed in 7.61s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/validation.py
```

Result: `Success: no issues found in 1 source file`.

## Gaps

None.
