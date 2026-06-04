# Classified Orphan Retention Validation

Plan reference: `plans/CLASSIFIED_ORPHAN_RETENTION_PLAN.md`

## Requirement Status

- Complete: Added focused regression coverage proving `sweep_classified_orphans()` passes a separate retention value to `workspace_id_view_from_session()`. Evidence: `tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py::test_sweep_classified_orphans_scans_classifies_and_reaps`.
- Complete: Added focused regression coverage proving production worker wiring supplies `completed_workspace_retention_hours` separately from `orphan_reconcile_min_age_hours`. Evidence: `tests/unit/service/test_worker.py::test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag`.
- Complete: Updated `sweep_classified_orphans()` to accept `min_retention_hours` and use it only for terminal workspace retention classification; `min_age_hours` remains the row-less missing-resource reap grace.
- Complete: Updated `build_worker_runtime()` to pass `settings.completed_workspace_retention_hours` into the classified-orphan sweep while preserving `settings.orphan_reconcile_min_age_hours` for missing-resource age.

## Evidence

Initial failing TDD check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py::test_sweep_classified_orphans_scans_classifies_and_reaps tests/unit/service/test_worker.py::test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag -q
```

Result: failed as expected. `sweep_classified_orphans()` rejected the new `min_retention_hours` keyword, and worker wiring did not pass `min_retention_hours`.

Final focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py::test_sweep_classified_orphans_scans_classifies_and_reaps tests/unit/service/test_worker.py::test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag -q
```

Result: passed, `3 passed in 0.86s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/orphan_resources.py src/awf/service/worker.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/service/test_worker.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/orphan_resources.py src/awf/service/worker.py
```

Result: passed.

Full AWF/GitHub validation and coverage were not run locally; AWF owns broad validation, provenance, and merge gating after agent completion.

## Gaps

None.
