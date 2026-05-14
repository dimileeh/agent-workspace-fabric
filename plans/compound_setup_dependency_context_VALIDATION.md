# Compound Setup Dependency Context Validation

Plan reference: `plans/compound_setup_dependency_context_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing a chained bootstrap failure with
  generic `fetch` wording is not classified as `SETUP_DEPENDENCY_NETWORK_FAILURE`.
  Evidence: `tests/unit/runtime/test_validation.py`.
- Complete: Preserve classification for chained commands when output includes
  package/index-specific dependency evidence. Evidence: existing
  `test_setup_dependency_network_classifier_accepts_chained_dependency_output`
  remains in the selected passing suite.
- Complete: Keep existing single-command dependency setup classification
  behavior intact. Evidence: selected setup dependency classifier suite passed.
- Complete: Run the narrow relevant unit tests and record the result.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k chained_bootstrap_fetch_failure`
  - Result: failed before implementation, passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier`
  - Result: passed, 43 passed and 143 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Files Changed

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/compound_setup_dependency_context_PLAN.md`
- `plans/compound_setup_dependency_context_VALIDATION.md`
