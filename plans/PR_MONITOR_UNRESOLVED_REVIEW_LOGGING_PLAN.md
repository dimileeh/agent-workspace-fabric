# PR Monitor Unresolved Review Logging Plan

## Problem

`monitor.action` logs report `unresolved_reviews` from
`len(PRStatus.unresolved_review_comments)`. That field intentionally retains
outside-diff review bodies and top-level PR comments for agent triage
bookkeeping, even after the monitor has handled them. Operators then see large
`unresolved_reviews` counts while GitHub has no unresolved review threads.

## Requirements

- Keep comment batching behavior unchanged.
- Keep raw retained outside-diff feedback visible as `review_feedback`.
- Make `unresolved_reviews` reflect feedback that still needs monitor/agent
  attention, using the existing pending-feedback state filter.
- Apply the same semantics to `monitor.pre_merge_recheck_changed_action`.
- Update tests so handled retained feedback no longer appears as unresolved.
- Do not commit the local fix.

## Implementation Steps

1. Add a regression test where a retained top-level review/issue comment has
   already been triaged, a later inline thread triggers `AddressComments`, and
   the log reports `review_feedback=1`, `pending_review_feedback=0`, and
   `unresolved_reviews=0`.
2. Update monitor logging in `src/awf/runtime/pr_monitor_runner/loop.py` and
   `merge_loop.py` so `unresolved_reviews` uses
   `_pending_review_feedback_count`.
3. Clarify the `PRStatus.unresolved_review_comments` docstring so future code
   does not reuse it as an unresolved GitHub review count.
4. Run targeted monitor logging tests, ruff, and mypy on touched Python files.
