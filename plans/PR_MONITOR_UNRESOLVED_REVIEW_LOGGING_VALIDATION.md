# PR Monitor Unresolved Review Logging Validation

Plan: `plans/PR_MONITOR_UNRESOLVED_REVIEW_LOGGING_PLAN.md`

## Requirement Status

- Keep comment batching behavior unchanged: Complete. No `AddressComments` or
  fix-cycle batching behavior changed.
- Keep raw retained outside-diff feedback visible as `review_feedback`:
  Complete. `review_feedback` remains `len(status.unresolved_review_comments)`.
- Make `unresolved_reviews` reflect feedback still needing attention: Complete.
  `monitor.action` now reports `unresolved_reviews` from
  `_pending_review_feedback_count`.
- Apply the same semantics to pre-merge recheck logs: Complete.
  `monitor.pre_merge_recheck_changed_action` uses the same pending count.
- Update tests: Complete. Added regression coverage for a handled retained bot
  comment plus a later inline thread.
- Do not commit the local fix: Complete. Changes remain uncommitted.

## Evidence

- Live investigation showed PR31/32/33/34 had zero unresolved GitHub review
  threads while old AWF logs reported nonzero `unresolved_reviews` from raw
  retained feedback.
- Failing regression reproduced the bug:
  `second_address["pending_review_feedback"] == 0` while
  `second_address["unresolved_reviews"] == 1`.
- After the fix, the regression passes with `unresolved_reviews == 0`.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_bot_issue_feedback_stays_alive_and_addresses_later_comments -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::test_pending_review_feedback_count_excludes_blocking_reviews_and_honors_state_hash tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::test_pending_review_feedback_count_includes_triageable_blocking_issue_comment -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/loop.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_monitor_action_logging.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/loop.py src/awf/runtime/pr_monitor_runner/merge_loop.py`
