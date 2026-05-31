# Review Comment Rollback Plan

## Problem Statement and Scope

PR review feedback reports that `_run_fix_cycle` can clear a review-level
comment's latest `needs_human` verdict after a non-workflow-scope push failure.
The bug appears when an earlier fix-cycle pass queues the same review comment in
`publish_dependent_ids` after `fix_committed`, then a later pass re-addresses
fresh comment evidence and upgrades the verdict to `needs_human` or
`agent_failed`.

Scope is limited to review-comment publish rollback bookkeeping in
`src/awf/runtime/pr_monitor_runner/fix_cycle.py` plus a focused regression test.

## Requirements Checklist

- Add a regression that fails when a review comment is `fix_committed` in one
  pass, re-addressed to `needs_human` in a later pass, and then encounters a
  generic push failure.
- Preserve the latest review-comment `needs_human` or `agent_failed` state and
  stored needs-human reason across generic push failure rollback.
- Keep existing rollback behavior for review comments whose latest verdict is
  publish-dependent, such as `fix_committed`.
- Avoid broad AWF/GitHub-owned validation; run only focused unit tests for the
  touched behavior.

## Implementation Steps

1. Add a focused unit regression in the PR monitor runner fix-cycle tests.
2. Run the new test and confirm it fails against the current implementation.
3. Mirror the inline-thread removal guard in the review-comment loop so a later
   `needs_human` or `agent_failed` verdict removes the comment id from
   `publish_dependent_ids`.
4. Run the focused regression and nearby targeted tests.
5. Record validation evidence in `plans/REVIEW_COMMENT_ROLLBACK_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k review_comment_needs_human`
  must fail before the implementation and pass after it.
- A focused nearby run for the edited test module must pass with the selected
  fix-cycle rollback tests.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
