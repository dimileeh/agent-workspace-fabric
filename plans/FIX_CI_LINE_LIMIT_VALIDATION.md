# Fix CI Line Limit Validation

Plan reference: `FIX_CI_LINE_LIMIT_PLAN.md`

## Requirement Status

- Keep all first-party code files at or below the configured line limit:
  Complete. The focused guard passed after splitting the oversized modules.
- Preserve the existing test coverage and behavior from the oversized modules:
  Complete. The moved executor and provisioner tests passed in their new split
  modules with the remaining original modules.
- Do not edit protected workflow, quality-gate, or configuration files:
  Complete. Changes are limited to test split files and plan/validation docs.
- Run focused verification only:
  Complete. Full AWF/GitHub validation and coverage gates were not run locally;
  AWF manages those after agent completion.
- Commit the fix locally with a conventional commit message:
  Complete. The final local commit for this fix is
  `fix(ci): line-limit guard — split oversized unit test modules`.

## Evidence

Files changed:

- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
- `plans/FIX_CI_LINE_LIMIT_PLAN.md`
- `plans/FIX_CI_LINE_LIMIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: passed, `1 passed in 0.59s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
  - Result: passed, `All checks passed!`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q`
  - Result: passed, `29 passed in 11.63s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py -q`
  - Result: passed, `45 passed in 9.66s`.
