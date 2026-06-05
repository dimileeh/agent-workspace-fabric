# Auth Overlay NULL-Order Markers Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6HWgzC` reports that the auth-overlay backfill
migration can skip inserting a real-order pending marker when an existing legacy
marker has `event_order IS NULL`, while the worker deferred sweep predicates use
plain `event_order >= -1` and therefore do not see that legacy marker.

Scope is limited to aligning the worker's current-cycle marker predicates with
the migration's `-1` floor behavior for legacy NULL-order release cycles, plus
focused regression coverage.

## Requirements Checklist

- Preserve migration behavior that treats NULL-order markers as current-cycle
  markers when the release cycle floor is `-1`.
- Make worker candidate listing detect legacy NULL-order pending markers when
  the release cycle floor is `-1`.
- Make worker pending-attempt counts include legacy NULL-order pending markers
  when the release cycle floor is `-1`.
- Make worker terminal-marker checks include legacy NULL-order resolved or
  exhausted markers when the release cycle floor is `-1`.
- Preserve existing current-cycle scoping for modern non-NULL release floors.
- Run focused tests only; broad AWF/GitHub validation remains owned by AWF after
  agent completion.

## Implementation Steps

1. Add focused failing tests for a NULL-order effective release with NULL-order
   pending and terminal markers.
2. Add a small SQL predicate helper in `cleanup_auth_overlay.py` that treats
   `event_order IS NULL` as in-cycle only when the coalesced release floor is
   `-1`.
3. Replace the worker marker `event_order >= release_cycle_floor` checks with
   the helper.
4. Run the targeted auth-overlay retry test file and any focused style/type
   checks needed for touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q`
- Optional focused lint if needed: `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup_auth_overlay.py tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`

Pass criteria: the targeted regression tests pass and no broad repository-wide
validation is run inside this agent phase.
