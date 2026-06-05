# Address PRRT_kwDOSJAM6s6HWLRP Validation

Plan reference: `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6HWLRP_PLAN.md`

## Requirement Status

- Verify the review against the current migration behavior: Complete. The
  reservation helper previously accepted only `workspace_id` and `cycle_floor`,
  so it could not recheck that the listed release event still owned the latest
  effective release cycle.
- Add focused regression coverage proving reservation is skipped when the latest
  effective release no longer matches the listed release event id/order:
  Complete. Added
  `test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation`.
- Add the locked release id/order recheck to the existing reservation update:
  Complete. `_reserve_workspace_event_order` now includes a latest effective
  release match predicate inside the atomic `UPDATE ... RETURNING`.
- Preserve the existing marker id, payload, idempotence, and current-cycle marker
  guard behavior: Complete. Pending marker insertion is unchanged, and existing
  marker/idempotence tests still pass.
- Run only focused tests/checks for the changed migration behavior: Complete.
  Full AWF/GitHub validation is intentionally left to AWF after agent completion.

## Evidence

- Initial regression check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation -q`
  failed because `_reserve_workspace_event_order` had no release id/order
  recheck contract.
- Focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation -q`
  passed.
- Focused reservation and SQLite migration behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_reserves_event_order_atomically tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_sqlite_parses_payloads_without_json_predicates -q`
  passed with 4 tests.
- Targeted Postgres migration behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_postgres_seeds_only_qualifying_rows_idempotently -q`
  passed.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  passed.
- Targeted type check:
  `uv run --python 3.12 --extra dev mypy migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`
  passed.

## Remaining Gaps

None for this review-thread scope. Broad validation and merge gating remain
owned by AWF/GitHub after agent completion.
