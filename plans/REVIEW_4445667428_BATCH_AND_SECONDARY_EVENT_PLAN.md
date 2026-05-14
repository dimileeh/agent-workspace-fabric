# Review 4445667428 Batch And Secondary Event Plan

## Problem Statement And Scope

Address review-level PR comment `issue:4445667428` for two focused event-stream
concerns:

- `WorkspaceRepository.add_events()` assigns the same `event_order` to every
  event in a multi-event batch, leaving same-timestamp events ambiguous.
- Cleanup failures recorded after a workspace is already failed currently use a
  synthetic `workspace.state_changed` event with `old_state == new_state ==
  failed`, which looks like a state-change self-loop.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Preserve existing single-event `add_event()` behavior.
- Assign deterministic, increasing `event_order` values within one
  `add_events()` batch.
- Replace the already-failed cleanup synthetic `workspace.state_changed` record
  with an event type that is not a lifecycle transition.
- Keep failure-causality snapshots able to recover secondary cleanup failures
  from the new event type.
- Add regression coverage before implementation and confirm the focused tests
  fail when practical.
- Run focused repository, controls, failure-causality, and lint checks.
- Write a validation document against this plan and commit the local fix with a
  conventional commit referencing the review comment id.

## Implementation Steps

1. Add or update tests for batched `add_events()` event ordering.
2. Update the already-failed cleanup test to require a dedicated secondary
   failure event and to reject synthetic `workspace.state_changed` self-loops.
3. Teach failure-causality lookup/history queries to include the dedicated
   secondary failure event type alongside failed state-change events.
4. Emit the dedicated secondary failure event from the already-failed cleanup
   path.
5. Update callback event policy only if the new event type must remain visible
   to existing `workspace.*` callback subscriptions.
6. Run focused verification and document results in
   `plans/REVIEW_4445667428_BATCH_AND_SECONDARY_EVENT_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_events.py -q`
  must pass if callback policy is touched.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/db/test_workspace_repository.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py`
  must pass.
