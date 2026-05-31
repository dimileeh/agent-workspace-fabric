# PRRT_kwDOSJAM6s6F6t_p Preserve Elapsed Settle Markers Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F6t_p` reports that monitor persistence can drop a
reviewer-settle done marker that was newly elapsed in the current monitor pass.
The stale-state merge helper currently removes any done marker when the locked
DB row has the matching started marker but lacks the done marker. That protects
re-armed freeze state from stale in-memory done markers, but it also removes a
freshly marked elapsed settle marker before it can be persisted.

Scope is limited to PR monitor state tracking/merge behavior, focused
regression coverage, and this plan/validation record.

## Requirements Checklist

- Add a regression showing `_persist_state` preserves a reviewer-settle done
  marker that `_non_check_reviewer_settle_decision` marked elapsed from the
  matching persisted started marker.
- Preserve the existing stale done-marker cleanup behavior for state that did
  not newly mark the done key in the current monitor pass.
- Keep the merge behavior generic enough for initial-review and reviewer-settle
  wait markers without broad refactors.
- Run only focused validation for touched behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add a failing regression near the existing operator freeze persistence tests.
2. Track monitor state keys written through `MonitorState.mark_addressed`.
3. Teach concurrent freeze-state merge to keep a DB-missing done key only when
   that done key was newly marked by the current in-memory monitor state.
4. Clear consumed dirty markers after successful state persistence so later
   stale writes are still cleaned up.
5. Run the targeted regression set and focused lint for changed files.

## Verification Commands And Pass Criteria

- Red:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k newly_elapsed_settle_done_marker`
  - Fails before implementation because the done marker is popped during
    persistence.
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "newly_elapsed_settle_done or stale_done_marker_when_freeze_started_matches or preserves_concurrent_operator_hint_and_freeze"`
  - Passes after implementation and proves fresh elapsed markers are preserved
    only when the DB started marker still matches.
- Focused style:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passes.
