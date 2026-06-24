# Orphan Reaper Status Validation

Plan reference: `plans/ORPHAN_REAPER_STATUS_PLAN.md`

## Requirement Status

- Verify worker/reaper liveness before turning an orphan failure into
  `ORPHANS_PRESENT_REAPING_ENABLED` success in `collect_service_status`:
  Complete.
- Keep orphan checks failing when auto-cleanup is enabled but the worker
  heartbeat is missing, stale, or unavailable: Complete.
- Preserve the existing auto-cleanup success state when the worker heartbeat is
  fresh: Complete.
- Keep the change scoped to status behavior and focused tests: Complete.

## Evidence

Files changed:

- `src/awf/service/status.py`
- `tests/unit/service/test_status_parts/test_status_part_001.py`
- `plans/ORPHAN_REAPER_STATUS_PLAN.md`
- `plans/ORPHAN_REAPER_STATUS_VALIDATION.md`

Focused red/green evidence:

- Initial regression run:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py::test_service_status_orphan_resources_requires_live_reaper_for_auto_cleanup -q`
  failed with `TypeError: collect_service_status() got an unexpected keyword argument 'worker_reaper_check'`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py::test_service_status_orphan_resources_requires_live_reaper_for_auto_cleanup -q`
  passed.
- Focused module:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py -q`
  passed with `34 passed`.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/status.py tests/unit/service/test_status_parts/test_status_part_001.py`
  passed.
- Narrow type check:
  `uv run --python 3.12 --extra dev mypy src/awf/service/status.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run inside this workspace phase.

## Remaining Gaps

None.
