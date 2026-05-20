# Review 4491715538 Diagnostic Accuracy Validation

Plan: `REVIEW_4491715538_DIAGNOSTIC_ACCURACY_PLAN.md`

## Requirement Status

- Preserve existing fail-closed quality-gate semantics: Complete.
  The protected edits are still violations; only diagnostic evidence changed.
- Report unchanged-`fail_under` coverage policy edits against `tool.coverage`: Complete.
  `test_pyproject_unchanged_coverage_fail_under_policy_change_is_specific` now asserts section `tool.coverage`.
- Keep lowered and raised `fail_under` diagnostics specific to `tool.coverage.report.fail_under`: Complete.
  The focused `coverage_fail_under` test run covered the lower, raise, unchanged, and duplicate unchanged policy cases.
- Report multiple unknown top-level pyproject section changes in one classifier pass: Complete.
  `test_pyproject_reports_all_unknown_top_level_section_changes` asserts both `custom` and `scripts` violations.
- Add or update focused regression tests before changing production code: Complete.
  The focused tests failed before implementation and passed after the classifier update.
- Run the narrow unit tests that prove the reviewer feedback is handled: Complete.
  See verification evidence below.
- Commit only the files changed for this review comment: Complete.
  The local commit includes only the two code/test files and this review comment's plan/validation files.

## Verification Evidence

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "unchanged_coverage_fail_under_policy_change_is_specific or reports_all_unknown_top_level_section_changes"`
  failed with the old `tool.coverage.report.fail_under` section and only one unknown top-level section.
- Focused green check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "coverage_fail_under or reports_all_unknown_top_level_section_changes"`
  passed with `4 passed, 94 deselected`.
- Quality gate unit suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `98 passed`.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- Format check:
  `uv run --python 3.12 --extra dev ruff format --check src/awf tests`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed with no issues.

## Files Changed

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_DIAGNOSTIC_ACCURACY_PLAN.md`
- `plans/REVIEW_4491715538_DIAGNOSTIC_ACCURACY_VALIDATION.md`
