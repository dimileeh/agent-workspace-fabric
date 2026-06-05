# Address PRRT_kwDOSJAM6s6HWLRP Plan

## Problem Statement And Scope

An inline review on PR #417 reports that the auth-overlay unmount backfill can
list an old latest effective `workspace.terminal_runtime_released` row, then
reserve a pending marker after a concurrent revoke plus newer release has
advanced the workspace to a new release cycle. The current reservation guard
checks only for current-cycle auth-overlay markers, so it can insert a stale
pending marker at or above the newer release floor.

Scope is limited to the migration's reservation guard and focused regression
coverage for the release-cycle race.

## Requirements Checklist

- Verify the review against the current migration behavior.
- Add focused regression coverage proving reservation is skipped when the latest
  effective release no longer matches the listed release event id/order.
- Add the locked release id/order recheck to the existing reservation update.
- Preserve the existing marker id, payload, idempotence, and current-cycle marker
  guard behavior.
- Run only focused tests/checks for the changed migration behavior; broad
  AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a regression test near the existing auth-overlay backfill migration tests.
2. Confirm the regression fails against the current code when practical.
3. Pass the listed release id/order into `_reserve_workspace_event_order`.
4. Extend the reservation update predicate so it returns no row unless the
   latest effective release is still the listed release.
5. Re-run the focused migration regression and nearby migration checks.
6. Record validation evidence in the matching validation artifact.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation -q`
  should fail before the fix and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_reserves_event_order_atomically tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_sqlite_parses_payloads_without_json_predicates -q`
  should pass after the fix.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  should pass after the fix.
- `uv run --python 3.12 --extra dev mypy migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`
  should pass after the fix.
