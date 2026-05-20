# Review 4491715538 Fail Under Type Plan

## Problem Statement And Scope

Address the review-level feedback from PR comment `issue:4491715538` about
`tool.coverage.report.fail_under` classification in protected `pyproject.toml`
diffs. Scope is limited to `src/awf/control/quality_gates.py`, focused unit
regressions, and the required plan/validation artifacts.

The comment reports two coverage-threshold cases:

- numeric `fail_under` raises are blocked;
- changing `fail_under` to a string/non-numeric value bypasses the policy.

Existing tests explicitly assert that unowned numeric threshold raises remain
blocked as a protected policy edit. Per repository safety policy, this plan
preserves that assertion and treats the raise complaint as conflicting review
feedback. The non-numeric/type-change bypass is in scope for a fix.

## Assumptions/Changes

- The focused regression showed the current implementation does not silently
  accept `fail_under = 99` to `fail_under = "99"`; it fails closed with a
  generic `tool.coverage` violation. The implementation scope is therefore
  narrowed to making this fail-closed case specific to
  `tool.coverage.report.fail_under` with a direct numeric-type reason.

## Requirements Checklist

- Preserve the existing behavior and regression test that unowned numeric
  `fail_under` raises are blocked with an ownership-policy reason.
- Add a failing regression for changing numeric `fail_under` to a non-numeric
  TOML value without any other coverage policy edits.
- Block `fail_under` additions, removals, or type/value changes when the old
  and new values are not both numeric, even if the rest of `tool.coverage` is
  unchanged after stripping `fail_under`.
- Keep diagnostic output specific to `tool.coverage.report.fail_under` where
  possible, including the file line for the new key when present.
- Run focused quality-gate tests and lint for the touched Python files.
- Commit the resulting changes locally on the current AWF-managed branch.

## Implementation Steps

1. Add a focused failing test in `tests/unit/control/test_quality_gates.py` for
   changing `fail_under = 99` to `fail_under = "99"`.
2. Run the new test to confirm it fails under the current classifier.
3. Update `_pyproject_policy_section_violations` to detect non-numeric
   `fail_under` value changes before comparing coverage policy with
   `fail_under` stripped.
4. Re-run the focused test, the nearby quality-gate coverage tests, and lint.
5. Document validation results in
   `plans/REVIEW_4491715538_FAIL_UNDER_TYPE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "fail_under"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.

All commands must pass, or any remaining gap must be documented in the
validation artifact.
