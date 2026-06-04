# Classified Orphan Reaper Production Wiring Validation

Plan reference: `plans/CLASSIFIED_ORPHAN_REAPER_PLAN.md`
Source contract: AWF workspace contract `ws_1fabedc3cd8b4d0a8c0655fb`, provided for this execution.
Related issues for PR context: #385, #386

## Requirement Status

- Complete: `auto_cleanup_orphans=False` prevents the classified reaper callback from firing. Covered by `test_classified_orphan_reaper_does_not_fire_when_auto_cleanup_off`.
- Complete: `auto_cleanup_orphans=True` allows interval-gated callback execution and rescheduling. Covered by `test_classified_orphan_reaper_fires_and_reschedules_when_auto_cleanup_on`.
- Complete: transient DB connection errors warn and reschedule; non-transient callback failures log and reschedule without blocking dispatch. Covered by the transient/fatal tests in `test_worker_part_046.py`.
- Complete: production `run_once()` invokes the new cleanup loop. Covered by `test_run_once_invokes_classified_orphan_reaper_loop`.
- Complete: production runtime wires a classified-orphan callback, compose teardown closure, Docker host, work dir, enable flag, min age, and interval config. Covered by `test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag`.
- Complete: classified scan-summary-to-reap production helper reaps a classified Docker/worktree orphan and preserves `ORPHAN_REAP_OK`. Covered by `test_sweep_classified_orphans_scans_classifies_and_reaps`.
- Complete: DB classification failure degrades to skipped-unknown instead of deleting. Covered by `test_sweep_classified_orphans_skips_when_workspace_view_unavailable`.
- Complete: config default and explicit setting flow from `Settings` to `ServiceSettings`. Covered by `test_orphan_reconcile_defaults_are_off_and_sane` and `test_orphan_reconcile_settings_flow_from_environment`.

## Files Changed

- `src/awf/common/config.py`
- `src/awf/service/config.py`
- `src/awf/control/worker/config.py`
- `src/awf/control/worker/constants.py`
- `src/awf/control/worker/manager.py`
- `src/awf/control/worker/cleanup.py`
- `src/awf/control/worker/mixins.py`
- `src/awf/service/worker.py`
- `src/awf/service/orphan_resources.py`
- `tests/unit/control/test_worker_parts/test_worker_part_046.py`
- `tests/unit/service/test_worker.py`
- `tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py`
- `tests/unit/service/test_config_parts/test_config_part_003.py`

## Evidence

Focused red run before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_046.py tests/unit/service/test_config_parts/test_config_part_003.py tests/unit/service/test_worker.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py -q
```

Result: failed as expected for missing `classified_orphan_reap_scan_interval_seconds`, missing `classified_orphan_reaper` worker wiring, missing `build_orphan_compose_teardown` import in `service.worker`, and missing `sweep_classified_orphans()`.

Focused final checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_046.py tests/unit/service/test_config_parts/test_config_part_003.py tests/unit/service/test_worker.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py -q
```

Result: `66 passed in 1.29s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/worker.py src/awf/service/orphan_resources.py src/awf/control/worker/manager.py src/awf/control/worker/cleanup.py src/awf/control/worker/mixins.py src/awf/control/worker/config.py src/awf/control/worker/constants.py src/awf/common/config.py src/awf/service/config.py tests/unit/control/test_worker_parts/test_worker_part_046.py tests/unit/service/test_worker.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/service/test_config_parts/test_config_part_003.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/worker.py src/awf/service/orphan_resources.py src/awf/control/worker
```

Result: `Success: no issues found in 25 source files`.

## Coverage Note

The focused tests exercise each new branch and production wiring path introduced here. Full repository coverage gates are intentionally not run inside this AWF agent phase; AWF/GitHub own broad validation, provenance, and coverage enforcement after agent completion.
