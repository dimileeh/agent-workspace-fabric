# CI Help Copy Regression Validation

Plan reference: `plans/CI_HELP_COPY_REGRESSION_PLAN.md`

## Requirement Status

- Complete: Reproduce the CI help-copy failure with a focused local selection.
  - Evidence: with CI-style color environment, the three help tests failed before
    the patch because Rich inserted ANSI styling inside `<path>`.
- Complete: Identify why the help output differs between isolated focused tests
  and the full coverage run.
  - Evidence: GitHub Actions forces colored Rich help; visible text remains
    `awf init <path>`, while raw output contains `awf init \x1b[1m<path>\x1b[0m`.
- Complete: Preserve public help guidance that points project onboarding at
  `awf init <path>`.
  - Evidence: tests now assert the unstyled visible help text still contains
    `awf service bootstrap` and `awf init <path>`.
- Complete: Keep the existing line-limit split intact.
  - Evidence: `tests/unit/cli/test_init_parts/test_init_part_001.py` is 1439
    lines and `test_init_part_005.py` remains 257 lines.
- Complete: Add or update focused regression coverage for the fix.
  - Evidence: the failing help tests now strip ANSI with `click.unstyle` before
    asserting visible help copy.
- Complete: Run focused verification commands only; leave full coverage to
  AWF/GitHub.
  - Evidence: only targeted pytest selections and ruff for touched test files
    were run locally.

## Files Changed

- `tests/unit/cli/test_init_parts/test_init_part_001.py`
- `tests/unit/cli/test_setup_commands.py`
- `tests/unit/cli/test_start_commands.py`
- `plans/CI_HELP_COPY_REGRESSION_PLAN.md`
- `plans/CI_HELP_COPY_REGRESSION_VALIDATION.md`

## Verification Evidence

- Failed before fix:
  - `CI=true GITHUB_ACTIONS=true TERM=xterm-256color FORCE_COLOR=1 CLICOLOR_FORCE=1 uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
- Passed after fix:
  - `CI=true GITHUB_ACTIONS=true TERM=xterm-256color FORCE_COLOR=1 CLICOLOR_FORCE=1 uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
  - `CI=true GITHUB_ACTIONS=true TERM=xterm-256color FORCE_COLOR=1 CLICOLOR_FORCE=1 uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_005.py -q`
  - `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py`

Full AWF/GitHub coverage validation remains managed by AWF after agent
completion.
