# Review 4445667428 Cleanup Secondary Event Plan

## Problem Statement And Scope

Review comment `issue:4445667428` identifies that the destroy cleanup callback
can drop cleanup failure causality when the workspace is already `failed` but no
primary failure snapshot is available. The current regression was written before
`workspace.secondary_failure_recorded` callback envelopes were sanitized; that
test now prevents the internal causality event from being recorded.

The same review also notes that old workers can double-advance
`workspaces.version` during the `event_order` migration rolling-deploy overlap.
That is an accepted monotonicity-preserving trade-off, but the migration should
make the operator assumption explicit.

## Requirements Checklist

- Update the cleanup regression so an already-failed workspace without primary
  evidence emits `workspace.secondary_failure_recorded` with secondary cleanup
  evidence.
- Keep the event payload free of `primary_failure` when no primary snapshot
  exists.
- Preserve the existing primary-preservation behavior when primary evidence is
  available.
- Document the migration rolling-deploy overlap trade-off in the migration.
- Run focused tests and lint for touched files.
- Commit the scoped fix locally and emit the required AWF verdict.

## Implementation Steps

1. Change the stale regression in `tests/unit/service/test_controls.py` to
   expect a secondary failure event and prove it fails before implementation.
2. Update `src/awf/service/controls.py` so the already-failed branch emits the
   secondary event even when primary evidence is absent, adding primary fields
   only when present.
3. Add a concise migration comment for the old-writer double-version window.
4. Re-run the focused cleanup tests, related callback/failure-causality coverage,
   and ruff.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_without_primary_evidence_records_secondary_event -q`
  fails before the implementation change and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_callbacks.py::test_secondary_failure_callback_envelope_excludes_internal_causality_payload tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls.py migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
  passes.
