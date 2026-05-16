# PRRT_kwDOSJAM6s6B76HJ Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6B76HJ_PLAN.md`

## Requirement Status

- Add a regression test for same-`occurred_at` failed state-change events where
  UUID order disagrees with chronological event order: Complete.
- Persist a workspace-local monotonic event ordering key for events that advance
  workspace lifecycle state or reset failure epochs: Complete.
- Update failure causality queries and same-timestamp epoch comparisons to use
  the monotonic ordering key when it is available: Complete.
- Preserve the existing conservative behavior for legacy rows that do not have
  the ordering key: Complete.
- Keep changes scoped to failure causality, event persistence metadata, and the
  required migration: Complete.

## Evidence

Files changed:

- `src/awf/db/models.py`
- `src/awf/db/repositories.py`
- `src/awf/service/controls.py`
- `src/awf/service/failure_causality.py`
- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/db/test_migration_graph.py`
- `plans/PRRT_kwDOSJAM6s6B76HJ_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6B76HJ_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_orders_same_timestamp_failures_by_event_order -q`
  - First run failed before implementation with `OLD_VALIDATION_FAILURE` selected
    instead of `CURRENT_VALIDATION_FAILURE`.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  - Passed: 20 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
  - Passed: 3 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_failure_causality.py tests/unit/db/test_migration_graph.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

No remaining planned gaps.
