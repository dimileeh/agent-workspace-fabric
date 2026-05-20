# PRRT_kwDOSJAM6s6DglLC Untrusted Env Expansion Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DglLC_UNTRUSTED_ENV_EXPANSION_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving added informational workflow
  steps reject `$PAT` and `$GH_PAT` shell expansion in `echo` or `printf` runs.
- Complete: Informational runs now fail closed for unbraced shell variable
  expansion unless the variable is known-safe or locally assigned earlier in
  the same informational run.
- Complete: Preserved existing allowances for literal informational prose,
  `$PATH`, and same-run literal variables such as `COV=85` followed by
  `echo "$COV"`.
- Complete: Ran focused quality-gate tests, the full quality-gate test module,
  touched-file lint, and focused type checking for the edited module.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DglLC_UNTRUSTED_ENV_EXPANSION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DglLC_UNTRUSTED_ENV_EXPANSION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_secret_bearing_expansions -q`
  failed before the implementation change with the new `$PAT` and `$GH_PAT`
  cases returning no violations.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_secret_bearing_expansions -q`
  passed after the implementation change with `17 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_informational_step_allows_cov_shell_variable_update tests/unit/control/test_quality_gates.py::test_added_informational_step_allows_echo_prose_validation_words tests/unit/control/test_quality_gates.py::test_informational_run_command_shell_safety_edges -q`
  passed with `34 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `298 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.

## Gaps

None.
