# Review 4585104324 Operator Hint Diff-Unavailable Status Validation

Plan reference:
`plans/REVIEW_4585104324_OPERATOR_HINT_DIFF_UNAVAILABLE_STATUS_PLAN.md`

## Requirement Status

- Complete: Preserve the existing terminal `_GitPushResult` returned for
  `ProtectedScopeDiffError`.
- Complete: Mark the pending operator hint as `needs_human` before returning
  the terminal diff-unavailable result.
- Complete: Use the protected-scope refusal message as the operator hint status
  reason when available.
- Complete: Preserve the existing `PROTECTED_SCOPE_PUSH_BLOCKED` needs-human
  behavior.
- Complete: Run only focused local validation; AWF/GitHub own broad validation
  after the agent phase.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/REVIEW_4585104324_OPERATOR_HINT_DIFF_UNAVAILABLE_STATUS_PLAN.md`
- `plans/REVIEW_4585104324_OPERATOR_HINT_DIFF_UNAVAILABLE_STATUS_VALIDATION.md`

Checks run:

- Failing-first evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result -q`
  failed before the production change because the hint status remained
  `pending`.
- Passing focused tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human -q`
  passed with `2 passed`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.

Broad AWF/GitHub-owned validation was not run during the agent phase, per the
workspace contract.
