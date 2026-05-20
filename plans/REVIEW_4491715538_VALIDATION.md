# Review 4491715538 Validation

Plan reference: `plans/REVIEW_4491715538_PLAN.md`

## Requirement Status

- Confirm whether explicit restricted informational job permissions are already
  accepted and covered by regression tests: Complete.
  `tests/unit/control/test_quality_gates.py` already includes
  `test_added_informational_job_with_restricted_permissions_is_allowed`, which
  covers `permissions: {}` and `permissions: contents: read`; the targeted test
  run passed.
- Add a failing regression for case-insensitive pinned action name comparison:
  Complete. Added
  `test_workflow_pinned_uses_bump_allows_action_case_change`; it failed before
  implementation because the classifier reported removed/added steps for the
  same differently-cased action.
- Implement the smallest code change that allows same-action pinned ref bumps
  when only action casing differs: Complete. `_step_identity` now normalizes
  parsed action names to lowercase for matching, and `_is_pinned_uses_bump`
  compares action names case-insensitively.
- Preserve existing coverage policy diagnostics unless a regression test proves
  the review comment identifies a real bug without conflicting with current
  policy tests: Complete. Existing
  `test_pyproject_fail_under_change_reports_other_coverage_policy_changes`
  remains unchanged and passing, preserving the policy that a `fail_under`
  change plus another coverage policy edit reports both blocked changes.
- Run relevant tests and lint: Complete.
- Commit the resulting changes locally on the current AWF-managed branch:
  Complete. The final local commit records the review fix on AWF's current
  branch.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_PLAN.md`
- `plans/REVIEW_4491715538_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'pinned_uses_bump_allows_action_case_change or restricted_permissions or fail_under_change_reports_other_coverage_policy_changes'`
  - First run failed on the new action-casing regression before implementation.
  - Final run passed: 4 passed, 215 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passed: 219 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Passed: All checks passed.

## Gaps

No remaining gaps for the actionable code change. The permissions concern is
stale in this checkout, and the coverage duplicate concern conflicts with an
existing regression that intentionally reports both a `fail_under` change and a
separate coverage policy edit.
