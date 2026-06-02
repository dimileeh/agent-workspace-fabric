# PRRT_kwDOSJAM6s6GUcB6 Plan

## Problem Statement And Scope

The planning-scope auto-retry path records
`workspace.planning_scope_auto_retry_blocked` when `retry_workspace_row`
cannot proceed because the failed source workspace has not emitted
`workspace.terminal_runtime_released`. The blocked payload names the release
event as the retry trigger, but no code currently resumes the auto-retry after
cleanup records that event.

Scope is limited to resuming the existing planning-scope auto-retry after
terminal runtime release and preserving existing retry safety checks and event
provenance.

## Requirements Checklist

- Add a regression test showing a blocked planning-scope auto-retry is retried
  after terminal runtime release.
- Keep `retry_workspace_row` as the code path that creates the replacement
  workspace.
- Record an auto-retry requested event when the deferred retry succeeds.
- Record an auto-retry failed event if the deferred retry is still blocked by
  normal retry errors.
- Avoid creating duplicate planning auto-retries if a retry was already
  requested.
- Run only focused checks for the changed behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add focused tests for blocked-and-resumed planning-scope auto-retry behavior.
2. Factor the planning-scope auto-retry request logic so the executor failure
   path and terminal-runtime-release path can share it.
3. Trigger the deferred planning-scope retry after the cleanup worker records
   `workspace.terminal_runtime_released`.
4. Add idempotence checks for existing planning auto-retry requested events.
5. Record validation evidence in the validation document.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q`

Pass criteria: the focused test module passes, including the new blocked-resume
regression. Full AWF/GitHub validation is intentionally not run inside the
agent phase.
