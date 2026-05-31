# CI Maintainability Line Limit Plan

## Problem Statement And Scope

PR #329 fails the first-party code line-limit guardrail:

- `src/awf/service/controls.py` has grown past `MAX_FIRST_PARTY_FILE_LINES`.
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py` has grown past `MAX_FIRST_PARTY_FILE_LINES`.

The scope is limited to decomposing those files without changing behavior or weakening the maintainability check.

## Requirements Checklist

- Preserve the existing maintainability guardrail unchanged.
- Keep `awf.service.controls` public imports compatible for existing callers and tests.
- Reduce every first-party code file to `<= 1_500` lines.
- Keep lifecycle test helper behavior equivalent after moving shared fixtures/helpers.
- Run only focused local checks; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Move workspace control exception classes from `src/awf/service/controls.py` into a focused `controls_errors` module.
2. Re-export those exception classes from `awf.service.controls` so existing import paths keep working.
3. Move shared lifecycle test fixtures/helpers from `test_controls_lifecycle_part_001.py` into a helper module under the same test package.
4. Update lifecycle test parts that imported helpers from part 001 to import from the helper module.
5. Re-run the reported maintainability test and the affected lifecycle test files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_004.py -q`
  - Passes to confirm moved lifecycle helpers remain compatible.
- Full AWF/GitHub validation is intentionally not run locally under the workspace contract.
