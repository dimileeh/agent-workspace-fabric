# PRRT_kwDOSJAM6s6F6mWa Drop Stale Done Markers Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F6mWa` reports that concurrent remonitor freeze
preservation can keep stale elapsed done markers when the persisted DB freeze
started marker is identical to the stale in-memory started marker. Scope is
limited to PR monitor state merge behavior and focused regression coverage.

## Requirements Checklist

- Add a regression test showing `_persist_state` removes stale initial-review
  and reviewer-settle done markers when the DB has re-armed matching started
  markers and no done markers.
- Preserve existing behavior that concurrent DB started markers are retained
  when stale monitor state is persisted.
- Keep unrelated monitor thread state intact.
- Run only focused validation for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add the failing regression near existing operator remonitor freeze tests.
2. Update the concurrent wait marker merge helper so stale done-marker cleanup
   runs whenever the DB has a started marker and lacks the corresponding done
   marker, including when the started marker is already considered preserved.
3. Run the targeted regression and an adjacent freeze-preservation test.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "stale_done_marker_when_freeze_started_matches or preserves_concurrent_operator_hint_and_freeze"`
  - Passes after implementation.
  - The new regression fails before the implementation because stale done
    markers remain persisted.
