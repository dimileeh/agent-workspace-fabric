# Pip Proxy Global Option Validation

Plan reference: `plans/PIP_PROXY_GLOBAL_OPTION_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `pip --proxy http://proxy:8080 install`
  is classified as a setup dependency network failure.
- Complete: Preserved adjacent package-manager parsing and transient failure
  classification behavior.
- Complete: Kept the production change narrow by adding `--proxy` to the
  existing value-taking dependency option set.
- Complete: Validated with the runtime validation unit test surface.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PIP_PROXY_GLOBAL_OPTION_PLAN.md`
- `plans/PIP_PROXY_GLOBAL_OPTION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_pip_proxy_before_subcommand -q`
  - First run failed before the production fix, proving the regression.
  - Second run passed after the production fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Passed: 202 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Passed.

## Gaps

None.
