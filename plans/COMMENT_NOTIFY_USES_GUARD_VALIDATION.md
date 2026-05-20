# Comment/Notify Uses Guard Validation

Plan reference: `COMMENT_NOTIFY_USES_GUARD_PLAN.md`

## Requirement Status

- Complete: Block newly added informational workflow steps that use untrusted
  comment/notify-looking actions.
  - Evidence: `test_added_informational_step_with_untrusted_notify_uses_is_blocked`.
- Complete: Block newly added informational workflow jobs that use untrusted
  comment/notify-looking actions in their steps.
  - Evidence: `test_added_informational_job_with_untrusted_notify_uses_is_blocked`.
- Complete: Preserve the existing allowance for known pinned PR comment actions.
  - Evidence: existing `test_added_informational_job_with_comment_action_uses_is_allowed`
    remains green.
- Complete: Keep protected workflow validation behavior otherwise unchanged.
  - Evidence: full `tests/unit/control/test_quality_gates.py` file remains green.

## Commands Run

- Initial expected failure:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Result: failed only the two new untrusted notify action regressions.
- Final verification:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Result: `65 passed`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Result: passed.

## Gaps

None.
