# Review 4445667428 Validation

Plan reference: `plans/REVIEW_4445667428_PLAN.md`

## Requirement Status

- Add a regression test proving cleanup failure on an already-failed workspace
  without primary evidence does not emit malformed
  `workspace.secondary_failure_recorded` events: Complete.
  - Added
    `test_destroy_cleanup_failure_without_primary_evidence_skips_secondary_event`
    in `tests/unit/service/test_controls.py`.
  - The tightened regression failed before implementation because the malformed
    secondary event was emitted.
- Keep existing preservation behavior for already-failed workspaces that do have
  primary evidence: Complete.
  - Re-ran
    `test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed`;
    it still records the structured secondary event with embedded primary
    evidence.
- Add or update migration regression coverage so same-timestamp historical
  events are backfilled by persisted insertion chronology rather than random
  uuid-derived IDs: Complete.
  - Updated
    `test_workspace_event_order_migration_backfills_existing_events` to expect
    insertion chronology for same-timestamp events whose IDs sort differently.
  - The test failed before implementation under the old `id ASC` tie-breaker.
- Update the migration backfill ordering without introducing branch changes,
  pushes, or unrelated refactors: Complete.
  - `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py` now uses
    `ctid ASC` as the one-time historical same-timestamp tie-breaker.
- Commit the scoped fix locally: Complete.
  - The scoped files are staged and committed after validation as the final
    local fix step.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `tests/unit/service/test_controls.py`
- `tests/unit/db/test_migration_graph.py`
- `plans/REVIEW_4445667428_PLAN.md`
- `plans/REVIEW_4445667428_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_without_primary_evidence_skips_secondary_event -q`
  - Initial regression attempt passed because it did not exercise the
    already-failed callback path; after tightening the scenario, it failed
    before implementation as expected and passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q`
  - Failed before implementation as expected and passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_without_primary_evidence_skips_secondary_event tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed -q`
  - Passed: 2 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q`
  - Passed: 1 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls.py tests/unit/db/test_migration_graph.py migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py -q`
  - Passed: 34 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
  - Passed: 6 passed.

## Gaps

No implementation gaps remain.
