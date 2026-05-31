# Comment 3329822768 Workflow Scope Requeue Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F67yP` reports that the comment repair
workflow-scope push-failure path preserves inline thread verdicts that still
need the post-push `resolve_thread()` step. Because GitHub rejects the push
before thread resolution, keeping `false_positive` or `defer` addressed state
can make later monitor decisions skip `AddressComments`: `false_positive` may
fall through toward merge while the thread remains unresolved, and `defer` may
remain stuck in `NotifyHuman` after workflow scope is fixed.

Scope is limited to `src/awf/runtime/pr_monitor_runner/fix_cycle.py` and the
focused workflow-scope regression tests.

## Requirements Checklist

- Requeue all publish-dependent inline thread verdicts on workflow-scope push
  failure, not only `fix_committed`.
- Preserve durable defer capture idempotency markers while clearing the verdict
  and body-hash state that controls `AddressComments`.
- Keep behavior focused to workflow-scope push failures; ordinary push failures
  already clear publish-dependent state.
- Add/update focused regression coverage for false-positive and defer inline
  thread states after workflow-scope rejection.
- Do not run broad AWF/GitHub-owned validation; record focused test evidence
  only.

## Implementation Steps

1. Update focused unit tests in
   `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
   to expect workflow-scope failures to clear publish-dependent inline verdicts
   and route unresolved threads back to `AddressComments`.
2. Run the targeted tests and confirm they fail against the current helper.
3. Update `_requeue_workflow_scope_publish_dependent_items()` so it clears
   inline thread ids that depend on post-push resolution and still clears
   unpublished `fix_committed` states.
4. Re-run the focused tests until green.

## Assumptions/Changes

- The fix should not clear every non-fix review-comment id in the generic
  publish-dependent set. Review-level false-positive resolutions are recorded
  before push and do not have a GraphQL `resolve_thread()` step, so the
  implementation tracks publish-dependent inline thread ids separately.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`

Pass criteria: the targeted workflow-scope monitor tests pass, with full
AWF/GitHub validation left to AWF after agent completion.
