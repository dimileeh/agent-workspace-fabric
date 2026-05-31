# PRRT_kwDOSJAM6s6F7q7b False Positive Push Failure Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F7q7b` reports that review-level
`false_positive` verdicts are now persisted only after a successful push. If the
push fails, the fix cycle clears their addressed state as publish-dependent, and
there is no durable `pr_feedback_resolutions` row for the next monitor poll to
restore.

Scope is limited to PR monitor review-comment false-positive persistence in
`src/awf/runtime/pr_monitor_runner/fix_cycle.py`, focused regression coverage,
and this plan/validation record.

## Requirements Checklist

- Persist review-level `false_positive` verdicts during the fix pass before the
  repair push can fail.
- Keep `fix_committed` review-comment verdicts recorded against the pushed head
  after a successful push.
- Preserve existing push-failure cleanup behavior for publish-dependent state.
- Add a focused regression proving a failed push still leaves a durable
  false-positive resolution that can restore monitor addressed state.
- Run only targeted local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Update the existing workflow-scope push-failure regression so it expects the
   review-comment false-positive resolution to be recorded and restorable after
   state cleanup.
2. Run that focused test to confirm it fails against the current implementation.
3. Move review-comment `false_positive` persistence back into the fix pass while
   leaving `fix_committed` queued for post-push recording.
4. Run the focused regression and adjacent review-comment persistence tests.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_restores_false_positive_review_comment_resolution -q`
  - Fails before the implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::test_review_comment_false_positive_is_recorded_by_pr_identity tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::test_review_comment_fix_committed_is_recorded_against_pushed_head -q`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run in this agent phase.
