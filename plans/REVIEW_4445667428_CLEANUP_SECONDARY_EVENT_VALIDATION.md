# Review 4445667428 Cleanup Secondary Event Validation

Plan reference: `plans/REVIEW_4445667428_CLEANUP_SECONDARY_EVENT_PLAN.md`

## Requirement Status

- Update the cleanup regression so an already-failed workspace without primary
  evidence emits `workspace.secondary_failure_recorded` with secondary cleanup
  evidence: Complete.
  - Renamed and updated
    `test_destroy_cleanup_failure_without_primary_evidence_records_secondary_event`.
  - The updated regression failed before implementation because no secondary
    failure event was emitted.
- Keep the event payload free of `primary_failure` when no primary snapshot
  exists: Complete.
  - The regression asserts the synthetic event carries `secondary_failure` and
    `secondary_failures`, but no `primary_failure`.
- Preserve the existing primary-preservation behavior when primary evidence is
  available: Complete.
  - Re-ran the existing already-failed primary-evidence cleanup regression.
- Document the migration rolling-deploy overlap trade-off in the migration:
  Complete.
  - Added an inline note that old writers can advance `workspaces.version`
    before the trigger reserves an `event_order`, leaving version ahead of the
    max event order while preserving monotonic event ordering.
- Run focused tests and lint for touched files: Complete.
- Commit the scoped fix locally and emit the required AWF verdict: Complete.
  - This validation file is included in the local fix commit, followed by the
    required AWF verdict line.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `tests/unit/service/test_controls.py`
- `plans/REVIEW_4445667428_CLEANUP_SECONDARY_EVENT_PLAN.md`
- `plans/REVIEW_4445667428_CLEANUP_SECONDARY_EVENT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_without_primary_evidence_records_secondary_event -q`
  - Failed before implementation as expected: no secondary event was recorded.
  - Passed after implementation: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_callbacks.py::test_secondary_failure_callback_envelope_excludes_internal_causality_payload tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q`
  - Passed: 3 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls.py migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py -q`
  - Passed: 34 passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: no issues found in 154 source files.

## Gaps

No implementation gaps remain.
