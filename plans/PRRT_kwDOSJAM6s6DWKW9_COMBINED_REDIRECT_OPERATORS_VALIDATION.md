# PRRT_kwDOSJAM6s6DWKW9 Combined Redirect Operators Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DWKW9_COMBINED_REDIRECT_OPERATORS_PLAN.md`

## Requirement Status

- Complete: Added informational workflow steps reject Bash combined redirection
  operators `&>`, `&>>`, `>&`, and `<&`.
- Complete: Existing safe informational output commands using `echo` and
  `printf` without redirection remain allowed.
- Complete: Existing command-separator behavior for safe informational commands
  remains unchanged.
- Complete: The change is covered by a failing regression before the production
  fix and validated after the fix.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DWKW9_COMBINED_REDIRECT_OPERATORS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DWKW9_COMBINED_REDIRECT_OPERATORS_VALIDATION.md`

Regression evidence:

- Before the production fix,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_combined_redirection_operators -q`
  failed with all four parameterized examples incorrectly producing no
  violations.

Validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_combined_redirection_operators -q`
  passed: `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_allows_echo_prose_validation_words tests/unit/control/test_quality_gates.py::test_workflow_informational_step_allows_cov_shell_variable_update -q`
  passed: `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed: `97 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.

## Gaps

None.
