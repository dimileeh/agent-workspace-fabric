# Collapse API Version Coverage Fix Validation

## Summary

Status: Complete locally.

The full `-n 20` coverage gate initially failed after the canonical v1 API
collapse because a few tests and compatibility helpers still assumed the
retired legacy v1 create semantics. A later full run exposed a parallel
Alembic DDL race in the test harness when multiple xdist workers ran live
Postgres migration subprocesses against the same control-plane database.

## Findings

- Canonical `POST /v1/workspaces` now creates task attempts and richer
  lifecycle events; stale tests that expected only a single `workspace.created`
  event were updated.
- Legacy-shaped workspace responses still need `env_profile` compatibility,
  so the service now derives it from the canonical profile reference when
  needed.
- Legacy persisted rows can contain only flat profile fields, so payload
  matching now accepts those rows without requiring a rich artifact.
- Token-unconfigured tests now explicitly clear `AWF_API_TOKEN` instead of
  relying on ambient environment behavior.
- Live Alembic subprocess tests now use a server/database-scoped file lock in
  the test harness. This serializes concurrent migration subprocesses that
  share one Postgres database while leaving product migration behavior
  unchanged.
- The final coverage shortfall was closed with focused edge tests rather than
  product-code padding.

## Validation

- Complete:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  - Result: `33 passed in 0.10s`.
- Complete:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_orders_old_writer_events_after_upgrade tests/integration/test_alembic_postgres.py::test_alembic_upgrade_downgrade_upgrade_on_postgres -n 3 --dist=loadscope -q`
  - Result: `3 passed in 8.12s`.
- Complete:
  `uv run --python 3.12 --extra dev ruff check scripts src/awf tests`
  - Result: passed.
- Complete:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: `Success: no issues found in 155 source files`.
- Complete:
  `uv run --python 3.12 --extra dev pytest -n 20 --dist=loadscope --cov=awf --cov-report=term-missing --cov-fail-under=99`
  - Result: `6518 passed, 1 skipped in 1433.95s`.
  - Coverage gate: `Required test coverage of 99% reached. Total coverage: 99.00%`.

## Notes

- The Alembic lock belongs in `tests/postgres.py`, not production migration
  code, because the race was caused by parallel local test harness behavior.
- The remaining commit/PR/rebuild/monitor steps are tracked in the plan and
  will be completed after this validation record.
