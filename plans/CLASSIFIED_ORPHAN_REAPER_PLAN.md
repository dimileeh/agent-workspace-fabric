# Classified Orphan Reaper Production Wiring Plan

Source contract: AWF workspace contract `ws_1fabedc3cd8b4d0a8c0655fb`, provided for this execution.

## Problem

`reap_classified_orphans()` is implemented and unit-tested, but the production worker only wires row-less directory reconciliation. With `auto_cleanup_orphans` enabled, classified Docker resources and worktrees tied to terminal or missing workspace rows are advertised as automatically reaped but have no worker call site.

## Scope

- Add a worker interval loop for classified-orphan reaping, matching the existing orphan-directory reconcile pattern.
- Wire production runtime construction through `src/awf/service/worker.py`.
- Add a small sweep helper in `src/awf/service/orphan_resources.py` to build Docker/worktree scans, classify against workspace rows, and call `reap_classified_orphans()`.
- Add a separate scan interval setting.
- Keep changes out of `auth_mounts.py` and `gc.py`.

## Requirements

- `auto_cleanup_orphans=False` must prevent the worker from firing the classified reaper callback.
- `auto_cleanup_orphans=True` must allow interval-gated callback execution and rescheduling.
- Transient DB connection errors are warned and rescheduled; non-transient callback errors are logged and rescheduled without blocking dispatch.
- Production wiring must build Docker and worktree scan summaries before reaping.
- Reason codes from `reap_classified_orphans()` remain intact.
- Focused tests cover loop firing, loop not firing, config flow, production callback wiring, and a classified Docker/worktree reap through the new sweep helper.

## Implementation Steps

1. Add `classified_orphan_reap_scan_interval_seconds` through `Settings`, `ServiceSettings`, and `WorkerConfig`, defaulting to `3600.0`.
2. Extend `ControlWorker` with an optional `classified_orphan_reaper` callback and next-scan cursor.
3. Add `_maybe_reap_classified_orphans()` to worker cleanup delegates and call it from `run_once()` near orphan-dir reconciliation.
4. Add `sweep_classified_orphans()` in `orphan_resources.py` to gather scans, load workspace classification view, build the summary, and call `reap_classified_orphans()`.
5. Wire `build_worker_runtime()` to pass the callback, compose teardown closure, and interval config.
6. Write validation artifact after focused checks; broad AWF/GitHub validation and full coverage are owned by AWF after agent completion.

## Focused Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_046.py tests/unit/service/test_worker.py tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py tests/unit/service/test_config_parts/test_config_part_003.py -q`
- `uv run --python 3.12 --extra dev ruff check <touched files and focused tests>`
- `uv run --python 3.12 --extra dev mypy src/awf/service/worker.py src/awf/service/orphan_resources.py src/awf/control/worker`

Full repository validation and coverage gates are intentionally deferred to AWF/GitHub per the workspace contract.
