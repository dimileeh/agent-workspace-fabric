# `awf service gc` no-DB-record orphan reclamation — Plan

Issue: dimileeh/agent-workspace-fabric#637
Type: additive bug fix (M). Strict TDD; honor the 99% coverage gate.

This is the tracked, protocol-conformant plan for #637 (validated by
`SERVICE_GC_NO_DB_RECORD_ORPHANS_VALIDATION.md`). It mirrors the AWF-generated
workspace plan (`docs/awf-plans/ws_a3c9e52c834e48b2b8c83e25.md`, intentionally
git-ignored) so the plan/validation pair lives in `plans/` per
`plans/PLAN_EXECUTION_PROTOCOL.md`.

## Problem & root cause (confirmed by reading the code)

The classification-driven reaper `reap_classified_orphans` / `sweep_classified_orphans`
(`src/awf/service/orphan_resources.py`) **already** reclaims row-less ("no-DB-record")
orphaned volumes + worktrees, acting on `classification in {"terminal","missing"}`. It is
*not* the thing that misses them. Two reachability gaps make it useless on demand:

1. **No on-demand entry point.** The reaper is only invoked from the worker's periodic loop
   `control/worker/cleanup.py:_maybe_reap_classified_orphans`, which early-returns unless
   `auto_cleanup_orphans=True` (default-off, `service/config.py`). `awf service gc` is
   DB-row-driven (candidate set = `select(Workspace)`), so it never sees row-less orphans;
   `awf workspace destroy` raises `WorkspaceNotFoundError` for them.
2. **Enumeration blind spot.** `orphan_resources.parse_docker_resource_rows` derives the
   workspace id only from the compose-project *label* (`workspace_id_from_project`). A
   `awf-ws_<id>-postgres_data` volume whose project label value is empty is dropped, even
   though `awf service status` (via `orphans.py`'s name fallback) already flags it.

The on-demand `gc --execute` path reaches the worker through a `service_gc_requests` row:
the API persists `pending` (`service/gc_request.py`), the worker claims + runs it
(`control/worker/cleanup_service_gc.py:_run_claimed_service_gc_trigger` → wired closures in
`service/worker.py` + `control/worker/manager.py`), and the worker's `report` dict is folded
back into the API response under `worker_reclaim.report` (`service/gc_worker_delegation.py`).
This is exactly the seam to hook the orphan reaper into.

## Requirements checklist

- Layer 1: reach the existing classified-orphan reaper from the on-demand gc path, forcing
  `enabled=True` for the operator-requested run.
- Layer 2: enumeration parity so the row-less `awf-ws_<id>-postgres_data` volume class is
  surfaced by `parse_docker_resource_rows`.
- Preserve every #637 safety constraint (reap only `terminal`/aged-`missing` +
  retention-expired; grace guard; scanner-availability skip; `volume_ready_workspace_ids`).
- No new typed gc-response field (item 3): the report already surfaces via the existing fold.

## Layer 1 — reach the existing reaper from the on-demand gc path

### `src/awf/service/worker.py` (≈ line 314, `_reap_classified_orphans` closure)
- Change the closure signature to accept a forced override:
  `async def _reap_classified_orphans(*, enabled: bool | None = None) -> OrphanReapResult:`
  then `resolved_enabled = settings.auto_cleanup_orphans if enabled is None else enabled` and
  pass `enabled=resolved_enabled` into `sweep_classified_orphans(...)`.
- Everything else unchanged: it already passes `min_age_hours=orphan_reconcile_min_age_hours`
  and `min_retention_hours=completed_workspace_retention_hours` (the SAME retention the issue
  requires us to reuse). The no-arg periodic call (`enabled is None`) keeps default-off
  behavior; the on-demand call passes `enabled=True`.

### `src/awf/control/worker/manager.py` (≈ line 98)
- Widen the `classified_orphan_reaper` param type from
  `Callable[[], Awaitable[OrphanReapResult]] | None` to
  `Callable[..., Awaitable[OrphanReapResult]] | None` so the keyword override type-checks.
  (Stored on `self._classified_orphan_reaper`; no other change.)

### `src/awf/control/worker/cleanup_service_gc.py` (`_run_claimed_service_gc_trigger`, ≈ 177–236)
- After the existing `report = await self._terminal_gc_reaper(**reaper_kwargs)` line and
  **inside the same guarded `try`** (so a raise is recorded on the row like a reaper failure,
  preserving the swallow-and-log / no-false-success contract), add the additive sweep:
  ```python
  if self._classified_orphan_reaper is not None:
      orphan_result = await self._classified_orphan_reaper(enabled=True)
      report = {**report, "classified_orphan_reap": orphan_result.to_dict()}
  ```
  - `enabled=True` is FORCED for this operator-requested run, regardless of
    `auto_cleanup_orphans`.
  - Folding into `report` means it flows untouched into `_finish_service_gc_trigger` →
    `service_gc_requests.result` → `worker_reclaim.report.classified_orphan_reap` in the API
    response (no change needed in `gc_worker_delegation.py`; `_log_terminal_gc_reap_summary`
    reads only its own keys and is unaffected).
  - Guard on `is not None`: the existing trigger tests build the worker without a
    `classified_orphan_reaper`, so they stay green (path skipped).
- Update the method docstring to note the additional on-demand orphan sweep.

### `src/awf/service/gc_request.py` / `gc_results.py` (item 3 — optional surface)
- **Decision: no typed-field change.** The reaped-orphan report already surfaces at
  `worker_reclaim.report.classified_orphan_reap` via the existing fold, so a new dataclass
  field would be redundant surface area + extra coverage burden against the "minimal scoped
  diff" rule. Leave both files untouched. (Revisit only if review explicitly wants a
  top-level count; the data is already present.)

## Layer 2 — enumeration parity sub-fix

### `src/awf/service/orphan_resources.py`
- Port the row-less volume **name** fallback from `orphans.py`
  (`_workspace_id_from_managed_name` / `_legacy_workspace_id_from_managed_tail` /
  `_infer_project_from_managed_name`). Add module-level regex constants mirroring `orphans.py`:
  `_WORKSPACE_ID_PATTERN = r"ws_[A-Za-z0-9][A-Za-z0-9_]*"`,
  `_WORKSPACE_ID_RE`, `_HYPHEN_DELIMITED_MANAGED_TAIL_RE`, plus a `_looks_like_workspace_id`
  helper and a focused `workspace_id_from_managed_volume_name(name) -> str | None`
  (legacy hyphen- then underscore-delimited tail; no `known_workspace_ids` branch needed —
  by definition a row-less orphan has no DB row to match).
- In `parse_docker_resource_rows`, when `workspace_id is None` **and** `kind == "volume"`
  **and** `not project`, retry via the name fallback and, on success, set
  `compose_project` from `_infer_project_from_managed_name(name, workspace_id)` (so the
  reaper tears down the right `awf-ws_<id>` project with `--volumes`). Keep all other kinds
  and the label-based path byte-for-byte unchanged.
- Net effect: a row-less `awf-ws_<id>-postgres_data` volume is now enumerated → classified
  `missing` (no DB row) → reaped by the existing `reap_classified_orphans` volume path
  (which already sets `remove_volumes=True` only when a volume record is itself cleanup-ready
  via `volume_ready_workspace_ids`).

## Tests to write first (TDD)

All under `tests/unit/` mirroring `src/`. Markers: `unit`.

1. **(b) Enumeration parity** — `tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_001.py`
   (the parse/scan part): `parse_docker_resource_rows("volume", <jsonl>)` with a row
   `{"name": "awf-ws_abc123-postgres_data", "project": "", "driver": "local"}` yields one
   `DetectedResource(kind="volume", workspace_id="ws_abc123", compose_project="awf-ws_abc123")`.
   Add negatives: a non-AWF volume name with empty project → dropped; a container/network row
   with empty project → still dropped (fallback is volume-only). Underscore-delimited variant
   `awf-ws_abc123_postgres_data` → also resolves (parity with `orphans.py`).

2. **(a) Reaper reaps a `missing` volume + worktree, leaves expected/within-grace** —
   `tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py`
   (reuse `_RecordingComposeTeardown`, `build_orphan_resource_summary`, `_ok_view`,
   `scan_managed_worktrees`). Build a summary from a row-less volume DetectedResource +
   a `ws_dead` worktree dir with `workspace_view=_ok_view()` (no rows ⇒ both `missing`);
   assert `reap_classified_orphans(..., enabled=True, min_age_hours=0)` tears down the
   compose project once with `remove_volumes=True` (volume record is cleanup-ready) and
   removes the worktree; and a parallel `expected`/within-grace volume is left untouched.
   (Existing tests already cover compose+worktree and young-missing skip; this adds the
   explicit volume-classified-`missing` case.)

3. **(c) Worker trigger folds the orphan reaper with `enabled=True`** —
   `tests/unit/control/test_worker_parts/test_worker_part_service_gc_trigger.py`.
   Extend `_make_worker` to optionally accept `classified_orphan_reaper`. New test: seed a
   pending row, wire a fake `_terminal_reaper` returning a report and a fake
   `_classified_orphan_reaper(*, enabled)` that records the `enabled` kwarg and returns an
   `OrphanReapResult(enabled=True, status="ok", reason_code=ORPHAN_REAP_OK, reaped=(...))`.
   After `_maybe_consume_service_gc_trigger()`: assert the orphan reaper was called with
   `enabled=True`, the row is `completed`, and `result["classified_orphan_reap"]["status"]
   == "ok"`. Add a second assertion that with `classified_orphan_reaper=None` (existing
   default) the report has no `classified_orphan_reap` key (back-compat).

4. **`_reap_classified_orphans` override branch** —
   extend `tests/unit/service/test_worker.py::test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag`
   to also call `classified_reaper(enabled=True)` and assert
   `created["classified_sweep_kwargs"]["enabled"] is True`, exercising the override branch
   alongside the existing no-arg (`enabled is None`) call. Covers both sides of the new
   conditional.

## Validation commands (focused — AWF/CI owns the broad gate)

```bash
# New/changed tests only
uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_service_gc_trigger.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py -q -k orphan
# Focused lint/type on touched files
uv run --python 3.12 --extra dev ruff check src/awf/service/orphan_resources.py \
  src/awf/service/worker.py src/awf/control/worker/cleanup_service_gc.py \
  src/awf/control/worker/manager.py
uv run --python 3.12 --extra dev ruff format --check <touched files>
uv run --python 3.12 --extra dev mypy
```
Full suite, 99% coverage aggregate, OpenAPI drift, and console gates are run by AWF/GitHub
CI after the agent phase — not here.

## Safety constraints honored (from #637)
- Reaper still acts ONLY on `classification in {"terminal","missing"}` AND
  retention-expired/aged; the `_missing_record_is_aged` grace guard is untouched (never reap
  within grace).
- `expected` (live row) and retained-terminal-within-168h salvage are never touched;
  `remove_volumes` stays driven by `volume_ready_workspace_ids` (False unless the volume
  record itself is cleanup-ready).
- Scanner-availability skip preserved: degraded docker/worktree scan ⇒ `skipped`, never reap
  on a partial inventory; DB-unavailable ⇒ `unknown` ⇒ skip.
- Idempotent: compose teardown no-ops on a down stack; `build_and_delete_gc_path` returns
  `PATH_ALREADY_REMOVED`.
- DB-driven `run_service_workspace_gc` candidate selection is **not** changed, and the
  default-off `auto_cleanup_orphans` periodic backstop is **not** changed — the on-demand
  sweep is purely additive and forces `enabled=True` only for the explicit operator request.

## Risks & assumptions
- **Orphan reaper inside the guarded try.** If `sweep_classified_orphans` raised, the gc
  request would be marked `failed`, losing the terminal-reaper report. In practice the reaper
  returns an `OrphanReapResult` (partial on errors) rather than raising, and DB/scanner
  failures degrade to `skipped`/`unknown`. Placing it in the try matches the existing
  no-false-success contract; accepted.
- **Volume teardown with a missing compose file.** A row-less volume's compose dir/file may
  be gone; `ComposeManager.teardown_project` is idempotent and the issue treats teardown as
  best-effort. Enumeration (the actual bug) is what this fix restores; reclamation rides the
  existing, already-tested reaper path.
- **Name-fallback scope.** Restricted to `kind == "volume"` with empty project, so
  containers/networks (which always carry the project label under the `--filter`) are
  unaffected and the existing label path is byte-for-byte preserved.
- **Coverage.** Each new branch (volume name fallback hit/miss, the `enabled` override both
  sides, the worker fold present/absent) has a matching focused test; no hollow tests, no
  gate lowering.

## Non-goals
- No change to `awf workspace destroy` (still row-driven; out of scope).
- No change to the periodic `auto_cleanup_orphans` backstop or its default.
- No change to DB-driven `run_service_workspace_gc` candidate selection.
- No new typed gc-response field (item 3 left as the existing `worker_reclaim.report` surface).
- No edits to protected quality-gate files (`pyproject.toml`, `.github/workflows/`, `.awf/`,
  `.coveragerc`, `setup.cfg`).
