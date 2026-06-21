# Fix CI PR 614 Line Limit Validation

Plan reference: `plans/FIX_CI_PR614_LINE_LIMIT_PLAN.md`

## Requirement Status

- Preserve all existing test behavior and assertions: Complete.
  `TestNotificationAndGraceHelpers` was moved unchanged into
  `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_011.py`.
- Reduce `test_pr_monitor_runner_part_005.py` below 1500 lines: Complete.
  `wc -l` reports 1399 lines for `test_pr_monitor_runner_part_005.py`.
- Keep the split local to adjacent `test_pr_monitor_runner_parts` files: Complete.
  Only `part_005` and new adjacent `part_011` were changed for tests.
- Do not weaken, skip, or disable the failing check: Complete.
  No CI, coverage, or maintainability configuration was changed.
- Run focused verification only: Complete.
  Broad AWF/GitHub validation was not run locally and remains managed by AWF after agent completion.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed: 1 test.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_011.py -q`
  passed: 2 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_011.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  passed: 20 tests.

## Gaps

None.
