# PR 268 Informational Step Executable Validation

Plan reference: `plans/PR268_INFORMATIONAL_STEP_EXECUTABLE_PLAN.md`

## Requirement Status

- Regression test proving label-only protected workflow steps are blocked: Complete.
  - Added `test_added_informational_step_requires_exactly_one_executable_key` in `tests/unit/control/test_quality_gates.py`.
  - Confirmed the new test failed before implementation with both no-executable and run-plus-uses cases accepted.
- Require informational steps to include exactly one executable key: Complete.
  - Updated `_is_informational_step()` in `src/awf/control/quality_gates.py` to reject steps unless exactly one of string-valued `run` or `uses` is present.
- Preserve existing allowed informational behavior: Complete.
  - Existing quality-gate unit coverage passed after the change.
- Run narrow verification: Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "test_added_informational_step_requires_exactly_one_executable_key"` passed.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q` passed with 217 tests.
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py` passed.
- Commit locally without pushing or switching branches: Complete.
  - This validation file is included in the local fix commit for the review thread.

## Remaining Gaps

None.
