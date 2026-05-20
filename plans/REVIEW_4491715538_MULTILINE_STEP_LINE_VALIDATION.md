# Review 4491715538 Multiline Workflow Step Line Validation

Plan reference:
`plans/REVIEW_4491715538_MULTILINE_STEP_LINE_PLAN.md`

## Requirement Status

- Complete: Added a regression proving a nameless multiline `run:` workflow
  step resolves to its own `run:` line and its own `continue-on-error:` line.
- Complete: Preserved the existing coverage-policy tests that block unowned
  raised `fail_under` values and report multi-dimensional coverage changes.
- Complete: Preserved the existing GitHub Actions expression tests that allow
  approved informational contexts and block untrusted PR title/head-ref values.
- Complete: Kept the code change scoped to quality-gate workflow line lookup.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_MULTILINE_STEP_LINE_PLAN.md`
- `plans/REVIEW_4491715538_MULTILINE_STEP_LINE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k line_lookup_helpers_cover_fallback_paths`
  failed before implementation with the new multiline `run:` assertion.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k line_lookup_helpers_cover_fallback_paths`
  passed after implementation: 1 passed, 249 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'line_lookup_helpers_cover_fallback_paths or github_actions_expression_echo or untrusted_github_event_expressions or raising_coverage_fail_under or fail_under_change_reports_other_coverage_policy_changes'`
  passed: 14 passed, 236 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed: 250 passed.
