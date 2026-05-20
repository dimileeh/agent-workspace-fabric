# Review 4491715538 Diagnostic Accuracy Plan

## Problem Statement

Greptile reported two diagnostic-quality issues in the protected pyproject classifier:

- coverage policy edits that leave `tool.coverage.report.fail_under` unchanged still report the violation section as `tool.coverage.report.fail_under`;
- `_pyproject_unknown_change_violations` returns after the first unknown top-level section change, hiding additional evidence from the operator.

The gate behavior should remain conservative and block the same unsafe edits, but violation evidence should identify the relevant section more accurately and report all unknown top-level changes found in one diff.

## Requirements

- Preserve existing fail-closed quality-gate semantics.
- Report unchanged-`fail_under` coverage policy edits against `tool.coverage`, not `tool.coverage.report.fail_under`.
- Keep lowered and raised `fail_under` diagnostics specific to `tool.coverage.report.fail_under`.
- Report multiple unknown top-level pyproject section changes in one classifier pass.
- Add or update focused regression tests before changing production code.
- Run the narrow unit tests that prove the reviewer feedback is handled.
- Commit only the files changed for this review comment.

## Implementation Steps

1. Update `tests/unit/control/test_quality_gates.py` expectations for unchanged coverage policy diagnostics.
2. Add a regression test for simultaneous unknown top-level pyproject section changes.
3. Run the focused tests to confirm the current implementation fails those expectations.
4. Update `src/awf/control/quality_gates.py` to emit the corrected coverage section and accumulate unknown-change violations.
5. Re-run focused tests, plus lint/type checks if the change shape justifies them.
6. Record validation evidence in `plans/REVIEW_4491715538_DIAGNOSTIC_ACCURACY_VALIDATION.md`.
