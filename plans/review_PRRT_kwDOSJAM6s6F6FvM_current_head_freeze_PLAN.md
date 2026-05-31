# PRRT_kwDOSJAM6s6F6FvM Current Head Freeze Plan

## Problem Statement and Scope

Past-settle remonitor currently re-arms reviewer-settle freeze markers for
head SHAs that already have elapsed settle entries in persisted monitor state.
When the PR branch has advanced, those elapsed entries may belong to an older
head while the open merge candidate records a newer current head. A no-reason
past-settle remonitor can therefore warn that merge is paused without arming
the freeze marker the monitor will evaluate for the current head.

Scope is limited to remonitor past-settle freeze target selection. Existing
stale `monitor_last_commit_sha` behavior must remain intact.

## Requirements Checklist

- Add a regression test for a no-reason past-settle remonitor where the open
  merge candidate head differs from the elapsed settle marker head.
- Preserve existing behavior that does not arm a stale `monitor_last_commit_sha`
  merely because another head has an elapsed settle marker.
- When past-settle state exists and an open merge candidate has a head SHA,
  re-arm reviewer-settle freeze for that candidate/current head as well as
  elapsed marker heads.
- Keep local validation focused; do not run broad AWF/GitHub validation.

## Implementation Steps

1. Add the service-level regression around no-reason remonitor and a current
   merge-candidate head without an elapsed marker.
2. Confirm the regression fails before implementation when practical.
3. Extend remonitor settle-head selection to accept an explicit current head
   and include it only when past-settle markers exist.
4. Read the open merge candidate during remonitor and pass its head SHA as the
   current-head freeze target.
5. Run focused tests for the changed remonitor behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_arms_current_candidate_head"`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle"`
  - Passes, proving the existing stale-SHA regression remains intact.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
