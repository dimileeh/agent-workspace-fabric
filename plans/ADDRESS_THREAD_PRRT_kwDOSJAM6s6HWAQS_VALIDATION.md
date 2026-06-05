# Address PRRT_kwDOSJAM6s6HWAQS Validation

Plan reference: `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6HWAQS_PLAN.md`

## Requirement Status

- Verify the review against the current migration behavior: Complete. The
  migration previously reserved an event order, then skipped insertion when the
  second marker check observed a current-cycle marker.
- Add a focused regression showing reservation does not advance
  `workspaces.event_sequence` when a current-cycle marker is already present:
  Complete. Added
  `test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears`.
- Update the migration so the current-cycle marker guard prevents the
  reservation itself from incrementing the sequence: Complete.
  `_reserve_workspace_event_order` now includes the marker guard in the
  `UPDATE ... RETURNING` and returns `None` when no reservation is made.
- Preserve the existing deterministic pending-event ID and idempotent backfill
  behavior: Complete. Pending-event ID generation is unchanged, and the targeted
  SQLite/Postgres backfill tests still pass.
- Run only focused tests/checks for the changed migration behavior: Complete.
  Full AWF/GitHub validation is intentionally left to AWF after agent
  completion.

## Evidence

- Initial regression check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears -q`
  failed because `_reserve_workspace_event_order` returned `5` instead of
  `None`.
- Focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears -q`
  passed.
- Focused migration selection:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_reserves_event_order_atomically tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_sqlite_parses_payloads_without_json_predicates -q`
  passed with 3 tests.
- Targeted Postgres migration behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_postgres_seeds_only_qualifying_rows_idempotently -q`
  passed.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  passed.

## Remaining Gaps

None for the scoped review-thread fix. Broad validation and merge gating remain
owned by AWF/GitHub after agent completion.
