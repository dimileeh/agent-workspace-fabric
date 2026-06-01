# Fix CI Executor Error Paths Line Limit Validation

Plan reference: `plans/fix_ci_executor_error_paths_line_limit_PLAN.md`

## Requirement Status

- Keep the maintainability guard intact: Complete.
- Move existing test coverage without changing executor behavior under test: Complete.
- Keep all first-party code files at or below 1500 lines: Complete.
- Run focused validation only; AWF/GitHub owns broad validation after agent completion: Complete.
- Commit the local fix without pushing or changing branches: Complete.

## Evidence

Files changed:

- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_014.py`
- `plans/fix_ci_executor_error_paths_line_limit_PLAN.md`
- `plans/fix_ci_executor_error_paths_line_limit_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: passed, `1 passed in 0.41s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_014.py -q`
  - Result: passed, `24 passed in 17.65s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_014.py`
  - Result: passed.
- `git diff --check`
  - Result: passed.
- `wc -l tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_014.py`
  - Result: part 013 has 1497 lines; part 014 has 66 lines.

This validation document is included in the local fix commit. Broad AWF/GitHub
validation was not run locally per workspace contract.
