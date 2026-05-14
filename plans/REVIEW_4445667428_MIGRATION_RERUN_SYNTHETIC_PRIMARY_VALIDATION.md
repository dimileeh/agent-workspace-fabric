# Review 4445667428 Migration Rerun And Synthetic Primary Validation

Plan reference:
`plans/REVIEW_4445667428_MIGRATION_RERUN_SYNTHETIC_PRIMARY_PLAN.md`

## Requirement Status

- Complete: Added
  `test_workspace_event_order_migration_reruns_after_column_exists`, which
  upgrades to the prior revision, simulates a partially advanced schema where
  `workspace_events.event_order` already exists, and proves upgrade to head
  backfills the row, advances `workspaces.version`, and creates the read index.
- Complete: Made the event-order migration rerunnable by using
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, backfilling only rows whose
  `event_order` is still `NULL`, creating the concurrent index with
  `if_not_exists=True`, and making downgrade cleanup tolerant with
  `if_exists`/`DROP COLUMN IF EXISTS`.
- Complete: Added
  `test_primary_failure_event_can_be_synthetic_secondary_record`, documenting
  that a `workspace.secondary_failure_recorded` event carrying embedded primary
  evidence can intentionally be selected as the current-epoch primary event.
- Complete: Added a `_FailureCausalityEvents` docstring explaining that
  synthetic secondary-failure events are an expected source for primary
  evidence because they preserve the embedded original primary snapshot.
- Complete: Focused tests and lint passed.
- Complete: Local commit is prepared after this validation file.
- Complete: The required `AWF-VERDICT` line will be emitted after the local
  commit.

## Evidence

Changed files:

- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `src/awf/service/failure_causality.py`
- `tests/unit/db/test_migration_graph.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/REVIEW_4445667428_MIGRATION_RERUN_SYNTHETIC_PRIMARY_PLAN.md`
- `plans/REVIEW_4445667428_MIGRATION_RERUN_SYNTHETIC_PRIMARY_VALIDATION.md`

TDD failure confirmed before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists tests/unit/service/test_failure_causality.py::test_primary_failure_event_can_be_synthetic_secondary_record -q
```

Result: failed as expected because rerunning the migration hit duplicate column
DDL for `workspace_events.event_order`; the synthetic primary-event regression
already passed because it documents intentional current behavior.

Focused verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists tests/unit/service/test_failure_causality.py::test_primary_failure_event_can_be_synthetic_secondary_record -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q
uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py src/awf/service/failure_causality.py tests/unit/db/test_migration_graph.py tests/unit/service/test_failure_causality.py
```

Results:

- New focused tests passed: 2 passed.
- Existing migration/causality focused tests passed: 3 passed.
- Ruff passed.

## Gaps

None.
