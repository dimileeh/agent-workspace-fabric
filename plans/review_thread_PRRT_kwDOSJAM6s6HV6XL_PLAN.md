# PRRT_kwDOSJAM6s6HV6XL Review Thread Plan

## Problem Statement and Scope

The auth-overlay unmount backfill migration reserves a workspace event order by
reading `workspaces.event_sequence` and later writing a fixed value. During live
upgrades, concurrent control-plane writers can advance the same workspace
sequence between those statements, letting the migration regress
`event_sequence` and reuse an `event_order`.

Scope is limited to the migration helper and a focused regression test for
atomic event-order reservation.

## Requirements Checklist

- Replace select-then-update event-order reservation with a single atomic
  `UPDATE ... RETURNING` that bases the new sequence on the current row value.
- Preserve the cycle-floor behavior used by the backfill.
- Keep SQLite migration test compatibility.
- Add focused regression coverage for the atomic reservation shape.
- Run only targeted checks; full AWF/GitHub validation remains post-agent work.

## Implementation Steps

1. Add a focused failing test that rejects a pre-update `event_sequence` read and
   verifies the PostgreSQL reservation update uses `greatest`.
2. Update `_reserve_workspace_event_order` to reserve via one row update with
   `RETURNING`.
3. Preserve SQLite support with a dialect-compatible scalar max expression.
4. Run the targeted migration test and focused lint/type checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q -k auth_overlay_unmount_backfill`
  passes.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  passes.
- `uv run --python 3.12 --extra dev mypy migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`
  passes.
