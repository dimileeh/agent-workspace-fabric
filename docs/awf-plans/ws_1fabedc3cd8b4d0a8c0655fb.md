# Classified Orphan Readiness Reaper Production Wiring Plan

Workspace: `ws_1fabedc3cd8b4d0a8c0655fb`
Task: Follow-up for AWF issue #361; address GitHub issues #385 and #386 in one PR.

## Problem Statement

`reap_classified_orphans()` is implemented and covered in isolation, but production never calls it. With `AWF_AUTO_CLEANUP_ORPHANS=true`, the worker currently only runs `reconcile_orphaned_workspace_dirs()` for row-less workspace directories. Classified Docker resources and worktrees tied to terminal or missing workspace rows remain on the node even though health/status readiness text advertises automatic teardown when the flag is enabled.

Issues read for context:

- #385 (`src/awf/service/worker.py` angle): the worker has no production call site for the readiness-driven classified-orphan reaper.
- #386 (`src/awf/service/orphan_resources.py` angle): `reap_classified_orphans()` is dead code outside unit tests, so Docker-level orphans are never reaped in deployment.

## Requirements Checklist

- Wire `reap_classified_orphans()` into production worker execution.
- Mirror the existing `_maybe_reconcile_orphan_dirs` interval-gated worker pattern.
- Build a Docker resource scan plus managed worktree scan summary before reaping.
- Gate destructive execution on `auto_cleanup_orphans`; when false, the new worker loop must not fire the reaper.
- Add a separate interval/config knob for the classified-orphan reaper.
- Preserve reason codes end-to-end in results/logs.
- Catch specific expected exceptions where the scan/classification path can degrade safely; leave unexpected fatal worker-loop failures logged and rescheduled, matching the existing orphan-dir loop style.
- Cover: loop firing when the flag is on, loop not firing when off, and classified Docker/worktree orphan reaping.
- Keep AWF core generic; do not touch Aira-specific behavior.
- Do not touch `auth_mounts.py` or `gc.py` overlay teardown.
- Reference #385 and #386 in the eventual PR description/commit context.

## Intended Files And Modules To Touch

Primary implementation surfaces:

- `src/awf/service/worker.py`: production runtime wiring; construct the classified-orphan reaper callback from existing settings, `ComposeManager`, Docker/worktree scanners, workspace DB classification, and `build_orphan_compose_teardown()`.
- `src/awf/service/orphan_resources.py`: add a small reusable sweep helper only if needed to keep `service/worker.py` thin; helper should build the summary and call `reap_classified_orphans()` without changing existing readiness payload semantics.

Worker loop support, because the existing `_maybe_*` pattern lives under `awf.control.worker` in this codebase:

- `src/awf/control/worker/manager.py`: accept/store an optional classified-orphan reaper callback and initialize its next-scan cursor.
- `src/awf/control/worker/cleanup.py`: add `_maybe_reap_classified_orphans()` mirroring `_maybe_reconcile_orphan_dirs`, but no-op when `auto_cleanup_orphans` is false.
- `src/awf/control/worker/mixins.py`: expose the new cleanup delegate.
- `src/awf/control/worker/config.py`: add the interval config field.
- `src/awf/control/worker/constants.py`: add worker-loop failure reason code(s) if the log path needs new constants.

Config flow for the new knob:

- `src/awf/common/config.py`: add a default and `Settings` field, likely `classified_orphan_reap_scan_interval_seconds`, defaulting conservatively to the existing orphan reconcile interval (`3600.0`).
- `src/awf/service/config.py`: thread the setting through `ServiceSettings` and `resolve_service_settings()`.

Focused tests:

- `tests/unit/control/test_worker_parts/test_worker_part_046.py` or the existing orphan cleanup part if preferred: worker-loop interval/flag/error handling for the new `_maybe_*` method.
- `tests/unit/service/test_worker.py`: production runtime wiring; assert the callback builds the expected scan/summary/reap call and the config interval flows into `WorkerConfig`.
- `tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py`: add or extend a focused test for the scan-summary-to-reap helper if introduced. Existing isolated tests already cover flag-off no-op and flag-on compose/worktree deletion.
- `tests/unit/service/test_config_parts/test_config_part_003.py`: config default and environment/settings flow for the new interval knob.

## Tests To Write First

1. `test_classified_orphan_reaper_does_not_fire_when_auto_cleanup_off`
   - Construct a `ControlWorker` with a reaper callback that would fail if invoked.
   - Set `WorkerConfig(auto_cleanup_orphans=False, classified_orphan_reap_scan_interval_seconds=...)`.
   - Call the new `_maybe_reap_classified_orphans()` directly.
   - Assert the callback was not called and the cursor remains immediately eligible for a future flag-on configuration.

2. `test_classified_orphan_reaper_fires_and_reschedules_when_auto_cleanup_on`
   - Monkeypatch `monotonic()` like the existing orphan-dir tests.
   - Configure `auto_cleanup_orphans=True` and an interval.
   - Assert one callback invocation, cursor reschedule, and no second invocation inside the interval.

3. `test_classified_orphan_reaper_transient_db_error_warns_and_reschedules`
   - Use the same closed-connection `InterfaceError` pattern as `test_worker_part_045.py`.
   - Assert no raise, warning reason code, and interval reschedule.

4. `test_classified_orphan_reaper_fatal_error_logs_and_reschedules`
   - Raise a non-transient exception from the callback.
   - Assert `_log.exception` event/reason code and no propagation, matching `_maybe_reconcile_orphan_dirs` behavior.

5. `test_build_worker_runtime_wires_classified_orphan_reaper`
   - Monkeypatch runtime dependencies as in `test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag`.
   - Capture the new callback and `WorkerConfig`.
   - Monkeypatch Docker scan, worktree scan, workspace view lookup, and `reap_classified_orphans()` to avoid Docker and real DB work.
   - Invoke the callback and assert:
     - Docker scan uses `settings.docker_host`.
     - Worktree scan uses `settings.work_dir`.
     - Summary is built with `auto_cleanup_orphans=True` when the setting is on.
     - `reap_classified_orphans()` receives `enabled=True`, the configured min-age, and the compose teardown closure.
     - The new interval setting is present in `WorkerConfig`.

6. `test_classified_orphan_reap_interval_defaults_and_flows_from_settings`
   - Extend config tests to assert default `3600.0` and explicit setting propagation from `Settings` to `ServiceSettings`.

7. If a new helper is added in `orphan_resources.py`, write its failing test before implementation:
   - Build fake Docker container/network/volume records plus a managed worktree for `ws_dead`.
   - Return a workspace view that classifies them as missing or terminal.
   - Assert one compose teardown and one worktree removal result with `ORPHAN_REAP_OK` when enabled.
   - Assert scanner-unavailable or DB-unavailable summaries produce `ORPHAN_REAP_SKIPPED_UNKNOWN`, not deletion.

## Implementation Steps

1. Add the classified-orphan reaper interval setting through `Settings`, `ServiceSettings`, and `WorkerConfig` with a default matching `orphan_reconcile_scan_interval_seconds` unless existing config conventions suggest otherwise.

2. Add the new worker callback slot:
   - `ControlWorker.__init__(..., classified_orphan_reaper: Callable[[], Awaitable[OrphanReapResult]] | None = None, ...)`.
   - Store it on `self` and initialize `self._next_classified_orphan_reap_scan_at = 0.0`.
   - Import `OrphanReapResult` under `TYPE_CHECKING` to avoid runtime coupling.

3. Add `_maybe_reap_classified_orphans()` in `awf.control.worker.cleanup`:
   - Return immediately when no callback is wired.
   - Return immediately when `self._config.auto_cleanup_orphans` is false; this satisfies the locked requirement that the new production reaper does not fire when the kill-switch is off.
   - Apply monotonic interval gating and reschedule after success or handled failure.
   - Treat transient DB connection exceptions with the existing `_worker_exception_is_transient_db_connection()` helper and `DB_CONNECTION_CLOSED_REASON`.
   - Log non-transient callback failures with a classified-orphan-specific reason code, swallow, and reschedule so one failed sweep does not block provisioning/dispatch.

4. Call `_maybe_reap_classified_orphans()` from `ControlWorker.run_once()` adjacent to `_maybe_reconcile_orphan_dirs()` so cleanup sweeps run before regular dispatch work.

5. In `src/awf/service/orphan_resources.py`, add a compact helper if it keeps callback wiring clearer, for example `sweep_classified_orphans(...)`:
   - Use `scan_docker_resources()` in `asyncio.to_thread()` with a subprocess runner to avoid blocking the worker loop on Docker CLI timeouts.
   - Use `scan_managed_worktrees(work_dir)`.
   - Use `workspace_id_view_from_session()` inside an async session from the worker's session factory; on `SQLAlchemyError`, build `unavailable_workspace_view()` so `reap_classified_orphans()` skips with an explicit reason rather than deleting on incomplete classification.
   - Build `OrphanResourceSummary` with `build_orphan_resource_summary(..., auto_cleanup_orphans=enabled)`.
   - Call `reap_classified_orphans(summary, work_dir=..., compose_teardown=..., enabled=enabled, min_age_hours=...)`.
   - Keep existing public readiness/status payload behavior unchanged.

6. In `src/awf/service/worker.py`, construct and pass the classified reaper callback:
   - Build `classified_orphan_teardown = build_orphan_compose_teardown(compose)`.
   - Capture `settings.docker_host`, `work_dir`, `settings.auto_cleanup_orphans`, and `settings.orphan_reconcile_min_age_hours`.
   - Pass `classified_orphan_reaper=_reap_classified_orphans` into `ControlWorker`.
   - Pass the new interval setting into `WorkerConfig`.

7. Keep logs generic and safe:
   - Log workspace ids, counts, statuses, and reason codes only.
   - Do not log environment, database URLs, tokens, or Docker host secrets.

8. Update validation artifact after implementation in the implementation phase, using the configured AWF plan/validation location rather than creating unrelated planning files during this planning-only phase.

## Validation Commands

Focused commands to run during implementation after the failing tests are written and the minimal code is added:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_worker_parts/test_worker_part_046.py \
  tests/unit/service/test_worker.py \
  tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py \
  tests/unit/service/test_config_parts/test_config_part_003.py \
  -q
```

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/service/worker.py \
  src/awf/service/orphan_resources.py \
  src/awf/control/worker/manager.py \
  src/awf/control/worker/cleanup.py \
  src/awf/control/worker/mixins.py \
  src/awf/control/worker/config.py \
  src/awf/control/worker/constants.py \
  src/awf/common/config.py \
  src/awf/service/config.py \
  tests/unit/control/test_worker_parts/test_worker_part_046.py \
  tests/unit/service/test_worker.py \
  tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py \
  tests/unit/service/test_config_parts/test_config_part_003.py
```

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/service/worker.py \
  src/awf/service/orphan_resources.py \
  src/awf/control/worker
```

Coverage handling:

- Do not run the full repository coverage gate in the agent phase; AWF/GitHub own broad validation and hard coverage after completion.
- Use the focused tests above to cover every new branch and record in the validation document which new lines/branches they exercise.
- If a local coverage spot-check is needed, keep it scoped to touched modules and do not use a repository-wide fail-under gate.

## Risks And Mitigations

- Risk: The prompt names `src/awf/service/worker.py` for the new `_maybe_*` loop, but the actual existing `_maybe_reconcile_orphan_dirs` pattern is implemented in `src/awf/control/worker/cleanup.py` and invoked from `ControlWorker.run_once()`.
  - Mitigation: Keep `service/worker.py` as the production wiring surface and add the interval loop in the existing control-worker cleanup module to preserve local architecture.

- Risk: Synchronous Docker scans could block the async worker loop.
  - Mitigation: Run Docker scan work through `asyncio.to_thread()` or an existing async runner pattern; keep CLI timeout behavior from `scan_docker_resources()`.

- Risk: Incomplete DB or scanner inventory could classify resources incorrectly.
  - Mitigation: Preserve existing summary semantics and rely on `reap_classified_orphans()` skipping unknown/unavailable summaries with `ORPHAN_REAP_SKIPPED_UNKNOWN`.

- Risk: Terminal workspaces within retention can have volumes classified as expected evidence.
  - Mitigation: Do not change existing `reap_classified_orphans()` volume handling; production wiring should pass the summary through unchanged.

- Risk: Adding a new config knob creates drift between `Settings`, `ServiceSettings`, and `WorkerConfig`.
  - Mitigation: Add focused default/flow tests and wire through one explicit field name.

- Risk: Broad validation and full coverage are requested by the issue context but disallowed inside the AWF agent phase.
  - Mitigation: Run focused tests/lint/type checks only and document that AWF/GitHub execute full validation and coverage gates after agent completion.

## Assumptions

- The new interval knob can default to `3600.0` seconds, matching existing orphan directory reconciliation, unless maintainers prefer a different value during review.
- `auto_cleanup_orphans` remains the single kill-switch for both row-less directory reconciliation execution and classified resource reaping.
- The worker should not build expensive Docker/worktree scans when `auto_cleanup_orphans` is false; readiness/status surfaces continue to provide non-destructive reporting outside the worker loop.
- The implementation may touch `awf.control.worker` and config modules even though the deferred issue paths are `service/worker.py` and `orphan_resources.py`, because that is where the existing worker `_maybe_*` pattern lives.

## Explicit Non-Goals

- No changes to `auth_mounts.py`.
- No changes to `gc.py` overlay teardown.
- No broad refactor of worker cleanup architecture.
- No changes to health/status/metrics advertised text unless a test proves it must be aligned with the new production call site.
- No manual GitHub push, branch switch, rebase, force-push, or PR creation.
- No full `.awf/workspace.yml` validation suite, whole-repository test suite, full frontend build, or full coverage gate during the agent phase.
