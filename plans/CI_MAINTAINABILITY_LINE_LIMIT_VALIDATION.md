# CI Maintainability Line Limit Validation

Plan reference: `plans/CI_MAINTAINABILITY_LINE_LIMIT_PLAN.md`

## Requirement Status

- Preserve the existing maintainability guardrail unchanged: Complete.
  - `tests/unit/test_core_decomposition_maintainability.py` was not edited.
- Keep `awf.service.controls` public imports compatible: Complete.
  - Control exception classes now live in `src/awf/service/controls_errors.py` and are imported/re-exported by `src/awf/service/controls.py`.
- Reduce every first-party code file to `<= 1_500` lines: Complete.
  - Focused maintainability repro passes.
- Keep lifecycle test helper behavior equivalent after moving shared fixtures/helpers: Complete.
  - Shared lifecycle scaffolding moved to `tests/unit/service/test_controls_lifecycle_parts/controls_lifecycle_helpers.py`.
  - Part 001 and part 004 lifecycle tests pass.
- Run only focused local checks: Complete.
  - Full AWF/GitHub validation was not run locally; AWF owns broad post-agent validation and merge gating.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `src/awf/service/controls_errors.py`
- `src/awf/service/controls_helpers.py`
- `tests/unit/service/test_controls_lifecycle_parts/controls_lifecycle_helpers.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_004.py`
- `plans/CI_MAINTAINABILITY_LINE_LIMIT_PLAN.md`
- `plans/CI_MAINTAINABILITY_LINE_LIMIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/controls_errors.py src/awf/service/controls_helpers.py tests/unit/service/test_controls_lifecycle_parts/controls_lifecycle_helpers.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_004.py`
  - Result: passed after applying import fixes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_004.py -q`
  - Result: passed, `39 passed`.
- `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py src/awf/service/controls_errors.py src/awf/service/controls_helpers.py`
  - Result: passed.

## Remaining Gaps

None for the planned scope.
