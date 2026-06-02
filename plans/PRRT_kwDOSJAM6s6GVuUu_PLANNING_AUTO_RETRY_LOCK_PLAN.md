# PRRT_kwDOSJAM6s6GVuUu Planning Auto-Retry Lock Plan

## Problem Statement

The planning-scope auto-retry path records `workspace.planning_scope_auto_retry_blocked`
after `retry_workspace_row` rolls back a runtime-not-released retry attempt. That rollback
releases the source workspace row lock. A concurrent manual retry can commit
`workspace.retry_requested` before the auto-retry path writes the blocked marker, causing
the blocked marker to become the latest terminal-release event and making cleanup create a
duplicate retry.

## Requirements

- Re-lock the source workspace row before recording the runtime-not-released blocked event.
- While holding that lock, do not record a blocked marker if a planning-scope retry request
  already won the race and is the latest relevant retry event.
- Preserve existing blocked-event behavior when no retry request has superseded it.
- Add a regression test for the manual-retry race.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add a failing unit test for the runtime-not-released branch where a matching
   `workspace.retry_requested` event is visible after the post-rollback lock.
2. Update the blocked-event branch to reload the workspace with `get_for_update`.
3. Add a small helper to read the latest matching planning-scope auto-retry terminal event
   and use it to suppress stale blocked markers after a retry request.
4. Reuse the helper in the pending-terminal-release check to keep event ordering logic in
   one place.
5. Run the focused unit tests covering planning auto-retry transactions.

## Verification

Targeted command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q
```

Pass criteria:

- The new regression fails before the code change.
- The targeted test file passes after implementation.
