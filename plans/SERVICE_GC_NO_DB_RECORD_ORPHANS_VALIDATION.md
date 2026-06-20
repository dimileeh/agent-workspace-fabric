# `awf service gc` no-DB-record orphan reclamation — Validation

Plan reference: `SERVICE_GC_NO_DB_RECORD_ORPHANS_PLAN.md`
Issue: dimileeh/agent-workspace-fabric#637
Implementation commit: `dcfab6054 fix(gc): reclaim no-DB-record orphans from on-demand awf service gc (#637)`

## Requirement Status

- Layer 1 — reach the existing classified-orphan reaper from the on-demand gc path:
  Complete.
  - `_run_claimed_service_gc_trigger` drives `self._classified_orphan_reaper(enabled=True)`
    after the DB-driven terminal pass and folds its `OrphanReapResult.to_dict()` into the
    report under `classified_orphan_reap`, inside the same guarded `try`
    (`src/awf/control/worker/cleanup_service_gc.py:229-236`).
  - Evidence: `test_consume_folds_classified_orphan_reaper_with_enabled_forced` asserts the
    reaper ran once with `enabled=True` and the folded report is present;
    `test_consume_omits_orphan_report_when_no_orphan_reaper_wired` asserts back-compat when
    no reaper is wired; `test_consume_marks_failed_when_orphan_reaper_raises` asserts a
    sweep raise marks the row `failed` with `SERVICE_GC_WORKER_RECLAIM_FAILED`
    (no false success).
- Forced-`enabled` override on the reaper closure for the on-demand path: Complete.
  - `_reap_classified_orphans(*, enabled: bool | None = None)` resolves to the
    `auto_cleanup_orphans` flag when `enabled is None` (periodic backstop) and to the forced
    value otherwise (`src/awf/service/worker.py:314-331`); param type on the worker dep
    widened to `Callable[..., ...]` (`src/awf/control/worker/manager.py:98`).
  - Evidence: `test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag` exercises
    both the no-arg (flag-default) and `enabled=True` (override) branches.
- Layer 2 — enumeration parity for the row-less `awf-ws_<id>-postgres_data` volume class:
  Complete.
  - `parse_docker_resource_rows` recovers the workspace id from the managed volume name and
    infers the compose project when `workspace_id is None and kind == "volume" and not
    project` (`src/awf/service/orphan_resources.py:508-525`); ported helpers
    `workspace_id_from_managed_volume_name` / `_managed_name_tail` /
    `_legacy_workspace_id_from_managed_tail` / `_infer_project_from_managed_name` /
    `_looks_like_workspace_id` mirror `orphans.py` (the `known_workspace_ids` branch is
    intentionally dropped — a row-less orphan has no DB row).
  - Evidence: `test_parse_volume_rows_recover_workspace_id_from_name_when_project_label_gone`,
    `_handles_underscore_delimited_tail`, `_infers_underscore_project_prefix`,
    `_drops_non_awf_volume`, `test_workspace_id_from_managed_volume_name_covers_legacy_name_shapes`,
    and `test_parse_name_fallback_is_volume_only_not_containers_or_networks`.
- Reaper reaps a `missing`-classified volume + worktree, leaves `expected`/within-grace:
  Complete.
  - Evidence: `test_reaper_reaps_missing_volume_via_name_fallback_and_leaves_expected`
    asserts the dead stack is torn down once with `remove_volumes=True`, the worktree is
    removed, and the live (`expected`) volume is untouched.
- Item 3 (typed gc-response field): intentionally not implemented per plan — the reaped
  report already surfaces at `worker_reclaim.report.classified_orphan_reap` via the existing
  fold; `gc_request.py` / `gc_results.py` left untouched (minimal-diff decision).
- Safety constraints (#637) preserved: only `terminal`/aged-`missing` + retention-expired
  reaped; `_missing_record_is_aged` grace guard, scanner-availability skip, and
  `volume_ready_workspace_ids`-driven `remove_volumes` are unchanged; DB-driven
  `run_service_workspace_gc` candidate selection and the default-off `auto_cleanup_orphans`
  periodic backstop are untouched.
- Review follow-up (PRRT_kwDOSJAM6s6LB30p) — on-demand sweep constrained to row-less orphans:
  Complete.
  - `reap_classified_orphans` / `sweep_classified_orphans` gained `row_less_only: bool = False`;
    when `True` only `missing` (no-DB-record) records are reaped, `terminal` records skipped
    (`src/awf/service/orphan_resources.py`). `_reap_classified_orphans` threads it through
    (`src/awf/service/worker.py`); the on-demand call passes `row_less_only=True`
    (`src/awf/control/worker/cleanup_service_gc.py`), the periodic backstop leaves it `False`.
  - Rationale: the on-demand sweep no longer tears down a terminal workspace the operator scoped
    out via `--status`/`--exclude-status` — those rows are already handled by the scope-honouring
    `_terminal_gc_reaper`; row-less orphans have no status to scope on.
  - Evidence: `test_reaper_row_less_only_skips_terminal_db_record_resources` (terminal stack left,
    row-less worktree reaped); `test_consume_folds_classified_orphan_reaper_with_enabled_forced`
    now asserts `row_less_only=True` is forwarded; the worker-wiring test asserts both the
    `False` (periodic) and `True` (on-demand) passthrough.

## Commands Run (focused — AWF/GitHub owns the broad gate)

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts tests/unit/control/test_worker_parts/test_worker_part_service_gc_trigger.py -q`
  - Passed: 89 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py -k orphan -q`
  - Passed: 2 tests (23 deselected).
- `uv run --python 3.12 --extra dev ruff check <touched src + test files>`
  - Passed: all checks passed.
- `uv run --python 3.12 --extra dev ruff format --check <touched src + test files>`
  - Passed: 8 files already formatted.
- `uv run --python 3.12 --extra dev mypy`
  - Passed: no issues found in 396 source files.

## Coverage reasoning (aggregate 99% gate is AWF/CI-owned)

The change is purely additive; every new line/branch has a focused test exercising it, so the
aggregate gate is not lowered:

- `src/awf/control/worker/cleanup_service_gc.py`: 100% under the focused trigger suite,
  including the orphan-fold present/absent branches and the guarded-raise path.
- `src/awf/service/orphan_resources.py`: the new name-fallback functions (`:1150-1203`) and the
  `parse_docker_resource_rows` fallback branch are fully covered by the layer-2 tests. The lines
  a focused-subset coverage run reports as missed (`workspace_id_view_from_session` /
  `_workspace_view_from_rows` DB-query helpers at `:1210-1214,1234->1236,1300,1399`) are
  **pre-existing** code covered by DB-integration suites outside this subset — not new code.
- `src/awf/service/worker.py`: the new `_reap_classified_orphans` override (`:314-331`) is
  covered; the focused-subset "misses" (`:343,414-474,540,630,639-641`) are other pre-existing
  `build_worker_runtime` closures covered elsewhere in the suite.

Full AWF/GitHub validation (whole suite, aggregate 99% coverage, OpenAPI drift, console) was not
run in the agent phase; AWF manages broad validation, provenance, logs, and merge gating after
agent completion.
