# PRRT_kwDOSJAM6s6F9az8 Pre-Push Coverage Reason Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F9az8_PRE_PUSH_COVERAGE_REASON_PLAN.md`

## Requirement Status

- Add a regression test for a pre-push coverage failure after successful
  `post_agent`/`validate` phases: Complete.
- Ensure returned push failure details expose the coverage reason code:
  Complete.
- Ensure the persisted validation run reason code uses the coverage reason:
  Complete.
- Preserve existing toolchain-missing and mixed-failure precedence behavior:
  Complete.
- Use only focused validation commands: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `plans/PRRT_kwDOSJAM6s6F9az8_PRE_PUSH_COVERAGE_REASON_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F9az8_PRE_PUSH_COVERAGE_REASON_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_coverage_failure_persists_coverage_reason_code -q`
  - Failed before implementation with `PR monitor pre-push validation failed: VALIDATION_OK`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Passed: 20 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Passed.

Full AWF/GitHub validation was not run locally; AWF owns broad validation,
provenance, and merge gating after agent completion.

## Remaining Gaps

None.
