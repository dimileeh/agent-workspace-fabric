# Address PRRT_kwDOSJAM6s6HWAQS Plan

## Problem Statement And Scope

An inline review on PR #417 reports that the auth-overlay backfill migration can
reserve a workspace event order, observe a concurrently inserted current-cycle
auth-overlay marker, and continue without inserting the corresponding event.
That leaves `workspaces.event_sequence` permanently ahead of the actual
workspace event history.

Scope is limited to the migration's event-order reservation path and focused
regression coverage for the sequence-leak race.

## Requirements Checklist

- Verify the review against the current migration behavior.
- Add a focused regression showing reservation does not advance
  `workspaces.event_sequence` when a current-cycle marker is already present.
- Update the migration so the current-cycle marker guard prevents the reservation
  itself from incrementing the sequence.
- Preserve the existing deterministic pending-event ID and idempotent backfill
  behavior.
- Run only focused tests/checks for the changed migration behavior; broad
  AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a regression test near the existing auth-overlay backfill migration tests.
2. Confirm the regression fails against the current code when practical.
3. Make `_reserve_workspace_event_order` return no reservation when a
   current-cycle marker exists, and remove the post-reservation second check.
4. Re-run the focused migration regression and related migration tests.
5. Record validation evidence in the matching validation artifact.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears -q`
  should fail before the fix and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_reserves_event_order_atomically tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_sqlite_parses_payloads_without_json_predicates -q`
  should pass after the fix.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  should pass after the fix.
