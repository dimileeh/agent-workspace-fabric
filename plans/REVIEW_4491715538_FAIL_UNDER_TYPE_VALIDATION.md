# Review 4491715538 Fail Under Type Validation

Plan reference: `plans/REVIEW_4491715538_FAIL_UNDER_TYPE_PLAN.md`

## Requirement Status

- Preserve existing numeric `fail_under` raise blocking: Complete.
  `test_pyproject_raising_coverage_fail_under_is_blocked_with_explicit_policy_reason`
  still passes and was not weakened.
- Add a regression for numeric-to-non-numeric `fail_under`: Complete.
  Added
  `test_pyproject_non_numeric_coverage_fail_under_change_is_blocked`.
- Block non-numeric `fail_under` value changes before stripping
  `fail_under` from coverage policy comparisons: Complete. The classifier now
  emits a specific `tool.coverage.report.fail_under` violation when old and new
  values differ and are not both numeric.
- Keep diagnostic output specific to the key and line: Complete. The new
  violation uses `tool.coverage.report.fail_under`, resolves the TOML key line
  when present, and reports that `fail_under` must remain numeric.
- Run focused tests and lint: Complete.
- Commit locally on the current AWF branch: Complete. The scoped changes are
  included in the local review-comment commit for `issue:4491715538`.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_FAIL_UNDER_TYPE_PLAN.md`
- `plans/REVIEW_4491715538_FAIL_UNDER_TYPE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "non_numeric_coverage_fail_under"`:
  failed before implementation because the violation was generic
  `tool.coverage`, confirming the regression test exercised the intended gap.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "fail_under"`:
  passed, 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`:
  passed, 266 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`:
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`:
  passed.

## Review Feedback Disposition

The reviewer's claim that numeric `fail_under` raises should be allowed
conflicts with an existing explicit safety regression and was preserved. The
non-numeric case was not silently accepted in the current code, but it was
reported generically; this change makes that fail-closed path specific and
actionable.
