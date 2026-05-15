# PRRT_kwDOSJAM6s6COa9S Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6COa9S_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for pip value-taking global options before
  `install`, including `--cache-dir`, `--log`, and `--retries`.
- Complete: Updated the dependency setup option-value skip list so those values
  are skipped before detecting pip dependency subcommands.
- Complete: Preserved existing package manager parsing behavior covered by the
  runtime validation unit suite.
- Complete: Validated with the narrow runtime validation test surface and
  targeted lint.
- Complete: Prepared the change for a local conventional commit tied to review
  thread `PRRT_kwDOSJAM6s6COa9S`.

## Evidence

- Added cases in
  `tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_pip_value_flags_before_subcommand`.
- Updated `_SETUP_DEPENDENCY_OPTION_VALUE_FLAGS` in
  `src/awf/runtime/validation.py`.
- Confirmed the new focused regression failed before the implementation:
  `7 failed, 3 passed`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_pip_value_flags_before_subcommand -q`
  (`10 passed`).
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  (`213 passed`).
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`.

## Remaining Gaps

None.
