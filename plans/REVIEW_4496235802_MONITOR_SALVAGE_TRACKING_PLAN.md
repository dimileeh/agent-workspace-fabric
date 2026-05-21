# Review 4496235802 Monitor Salvage Tracking Plan

## Problem Statement And Scope

Greptile's review-level comment for PR #272 reports four preserved-active
recovery concerns. The current branch already contains fixes and regressions
for malformed branch PR payloads, clean no-commit preservation grace, and
active-status preservation lookup. The remaining applicable follow-up is to
bound the worker-session memory used to remember active salvage monitor recovery
operation IDs.

Scope is limited to `ControlWorker`'s in-memory active-salvage monitor recovery
operation tracking and focused unit coverage.

## Requirements Checklist

- Confirm existing branch behavior already covers the first three review issues.
- Keep active salvage monitor recovery operation tracking scoped to a worker
  session, but prevent unbounded growth within long-running sessions.
- Preserve existing monitor resume cooldown behavior for tracked operation IDs.
- Add a regression proving old operation IDs are evicted when the worker exceeds
  the tracking bound.
- Run the narrowest relevant unit tests for the changed worker behavior.

## Implementation Steps

1. Add a bounded, insertion-ordered tracker for active salvage monitor recovery
   operation IDs.
2. Replace direct set mutation with helper methods for remembering and
   forgetting operation IDs.
3. Add a focused unit test that seeds more than the bound and verifies oldest
   entries are evicted while recent entries remain.
4. Run targeted worker tests and update validation notes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'active_salvage_monitor_recovery_operation_ids or dispatch_helpers_respect_limits_and_existing_tasks or preserved_active_pr_handoff_attaches_one_monitor_after_restart'`
  must pass.
