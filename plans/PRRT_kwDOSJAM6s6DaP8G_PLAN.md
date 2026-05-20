# PRRT_kwDOSJAM6s6DaP8G Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6DaP8G` flags repeated stale-active-execution salvage checks in `src/awf/control/worker.py` and calls out that state-dependent salvage idempotency checks must be scoped to the current workspace status or phase.

## Scope

- Keep the fix limited to active-execution salvage idempotency checks.
- Preserve the existing session-passing pattern; do not open sessions inside salvage-check loops.
- Do not change branch state, push, or PR-thread state manually.

## Requirements

- Add regression coverage proving a salvage event from another workspace status does not suppress stale-active-execution failure for the current status.
- Scope current salvage-event idempotency checks to the salvage payload's `workspace_status`.
- Refactor the reviewed repeated stale-failure salvage checks into an iterable collection.
- Update all callers of the salvage-event helper so idempotency remains status-aware.
- Commit the change locally with a conventional commit message tied to the thread id.

## Implementation Steps

1. Add a failing regression test for stale-active-execution failure when only a mismatched-status salvage event exists after the preservation event.
2. Update `_has_current_salvage_event` to require the current `WorkspaceStatus` and filter on `payload["workspace_status"]`.
3. Pass the candidate status at each helper call site.
4. Replace the reviewed repeated stale-failure checks with a loop over salvage event/reason pairs.
5. Run the focused regression test, then run targeted worker tests and static checks appropriate to the touched surface.
6. Record validation evidence in `plans/PRRT_kwDOSJAM6s6DaP8G_VALIDATION.md`.
