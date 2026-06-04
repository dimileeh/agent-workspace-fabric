# Classified Orphan Retention Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6HAA9G` reports that the production classified-orphan reaper uses `orphan_reconcile_min_age_hours` both as the row-less missing-resource grace window and as the terminal workspace retention window. Health and metrics classify retained terminal salvage with `completed_workspace_retention_hours`, so the worker can delete terminal volumes or worktrees before readiness still considers them protected evidence.

## Scope

- Keep the existing row-less missing-resource age guard driven by `orphan_reconcile_min_age_hours`.
- Classify terminal workspace salvage in the worker reaper with `completed_workspace_retention_hours`.
- Keep the fix limited to classified-orphan sweep wiring and focused regressions.
- Do not run broad AWF/GitHub validation; AWF owns full validation after agent completion.

## Requirements Checklist

- [ ] Add focused regression coverage proving `sweep_classified_orphans()` passes a separate retention value to `workspace_id_view_from_session()`.
- [ ] Add focused regression coverage proving production worker wiring supplies `completed_workspace_retention_hours` separately from `orphan_reconcile_min_age_hours`.
- [ ] Update `sweep_classified_orphans()` to accept and use separate classification retention and reaper missing-age values.
- [ ] Update `build_worker_runtime()` to pass both settings to the classified-orphan sweep.

## Implementation Steps

1. Update unit tests first so the current implementation fails on the separated-retention contract.
2. Add a `min_retention_hours` keyword to `sweep_classified_orphans()` and use it for `workspace_id_view_from_session()`.
3. Wire `settings.completed_workspace_retention_hours` into `_reap_classified_orphans()` in `service/worker.py`.
4. Run focused tests for the changed orphan-resource and worker wiring behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py::test_sweep_classified_orphans_scans_classifies_and_reaps tests/unit/service/test_worker.py::test_build_worker_runtime_wires_cleanup_callbacks -q`

Pass criteria: both focused tests pass after failing before the production code change, and no broad validation is run locally.
