# Address Review Comment 4590903660 Notification Wording Validation

Plan reference: `ADDRESS_REVIEW_4590903660_NOTIFICATION_WORDING_PLAN.md`

## Requirement Status

- Change the notification text so exhausted method attempts do not imply that
  an allowed method was disallowed by policy: Complete.
- Preserve existing fields: `attempted`, `effective_allowed`, optional GitHub
  detail, and the 2000-character cap: Complete.
- Add focused test coverage for the generic GitHub rejection with no remaining
  alternative: Complete.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`
- `plans/ADDRESS_REVIEW_4590903660_NOTIFICATION_WORDING_PLAN.md`
- `plans/ADDRESS_REVIEW_4590903660_NOTIFICATION_WORDING_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passed: 14 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passed.

Full AWF/GitHub validation was not run locally per workspace contract; AWF owns
broad validation, provenance, logs, timeouts, and merge gating after agent
completion.
