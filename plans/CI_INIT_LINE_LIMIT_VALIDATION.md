# CI Init Line-Limit Fix Validation

Plan reference: `plans/CI_INIT_LINE_LIMIT_PLAN.md`

## Requirement Status

- Complete: Existing init CLI behavior assertions remain covered. The moved
  env-header tests pass from the new split module.
- Complete: `test_init_part_001.py` is now 1,437 lines, below the 1,500-line
  guardrail.
- Complete: `test_init_part_005.py` is 257 lines, below the 1,500-line
  guardrail.
- Complete: The maintainability check remains unchanged and now passes in the
  AWF-provided focused repro.
- Complete: Only focused local verification was run. Full AWF/GitHub validation
  remains managed by AWF after agent completion.

## Evidence

- Files changed:
  - `tests/unit/cli/test_init_parts/test_init_part_001.py`
  - `tests/unit/cli/test_init_parts/test_init_part_005.py`
  - `plans/CI_INIT_LINE_LIMIT_PLAN.md`
  - `plans/CI_INIT_LINE_LIMIT_VALIDATION.md`
- `wc -l tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_005.py`
  - `test_init_part_001.py`: 1,437 lines
  - `test_init_part_005.py`: 257 lines
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_005.py -q`
  - Passed: 3 tests
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
  - Passed: 4 tests
- `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_005.py`
  - Passed

## Remaining Gaps

None for the planned scope.
