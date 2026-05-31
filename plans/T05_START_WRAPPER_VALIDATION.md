# T05 — Add `awf start` Wrapper Over Existing Service Bootstrap (Validation)

Plan reference: `plans/T05_START_WRAPPER_PLAN.md`
AWF planning artifact: `docs/awf-plans/ws_54c716f206e8484ca946e4fa.md`

This record closes the Plan-and-Validate loop required by
`plans/PLAN_EXECUTION_PROTOCOL.md` §3/§5. The implementation landed in commit
`ad9ade2d` ("awf: T05 - Add awf start wrapper over existing service bootstrap")
with follow-ups `cb99efe2` (root `.env` fallback on the source-checkout path) and
`73fac641` (extract bootstrap reason codes to named constants); this validation
doc was added afterwards so the post-implementation gap check is on record.

## Requirement Status

- Real `awf start` that delegates to `run_service_bootstrap` (no reimplementation
  of service startup): **Complete**.
  - `start_command` in `src/awf/cli/start_commands.py` builds
    `ServiceBootstrapOptions` and calls `run_service_bootstrap(...)` via the
    existing wiring (`_resolve_service_compose_paths`,
    `_resolve_service_runtime_env_files`, `local_service_environ`,
    `resolve_service_settings`).
- Public interface: `--rebuild`, `--skip-agent-runtime-build`,
  `--timeout-seconds N` (`min=0.0`, default `180.0`), `--source-checkout PATH`,
  `--format json|pretty` (default pretty): **Complete**.
  - Options defined on `start_command`; covered by
    `test_start_help_advertises_options_and_local_core` and
    `test_start_maps_flags_to_bootstrap_options`.
- `--rebuild` mutually exclusive with `--skip-agent-runtime-build`; passing both
  exits code 2 and never starts Core: **Complete**.
  - Guard at the top of `start_command` (exit code 2 before any wiring);
    covered by `test_start_rebuild_conflicts_with_skip`.
- Asset selection precedence — explicit `--source-checkout` →
  stored host-setup config metadata (revalidated, fails loudly when stale) →
  default discovery; no silent fallback: **Complete**.
  - `_resolve_start_source_checkout` / `_resolve_start_bootstrap_inputs`; covered
    by `test_start_with_valid_source_checkout_pins_assets`,
    `test_start_stale_stored_metadata_fails_without_fallback`,
    `test_start_no_stored_metadata_uses_default_discovery`,
    `test_start_tolerates_corrupt_config_without_source_request`, and
    `test_start_with_invalid_source_checkout_fails_without_bootstrap`.
  - Source-checkout root `.env` fallback (so tokens/DB settings are read before
    the compose `.env` is seeded, without forwarding the root `.env` to Docker):
    **Complete** — covered by
    `test_resolve_start_inputs_source_checkout_falls_back_to_root_env`.
- Backward-compatible bootstrap hooks (`src/awf/service/bootstrap.py`):
  `asset_root: Path | None = None` pinning asset resolution, and
  `force_rebuild: bool = False` adding `--no-cache` to the agent-runtime build:
  **Complete**.
  - `run_service_bootstrap` threads `asset_root` into `_resolve_bootstrap_assets`
    (pinned via `_resolve_pinned_bootstrap_assets`, invalid root raises
    `SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND`); `force_rebuild` appends `--no-cache`
    to the `agent_runtime_build` stage. Defaults preserve current behavior.
    Covered by the bootstrap-parts suite.
- Failure translation preserves structured diagnostics under
  `details["bootstrap"]` and maps `SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND →
  START_COMPOSE_ASSETS_MISSING`, `SERVICE_BOOTSTRAP_TIMEOUT →
  START_HEALTH_TIMEOUT`, `migrate` stage failure → `START_MIGRATION_FAILED`,
  port-bind signature → `START_PORT_CONFLICT`, and echoes the original
  `reason_code` otherwise (no dropped failures): **Complete**.
  - `_start_failure_payload` / `_classify_start_failure` /
    `_unclassified_start_failure_payload`; covered by
    `test_start_migration_failure_preserves_diagnostics`,
    `test_start_port_conflict_maps_reason_code`,
    `test_start_timeout_maps_to_health_timeout`,
    `test_start_assets_not_found_maps_to_compose_missing`,
    `test_start_unclassified_stage_failure_echoes_reason_code`, and
    `test_start_failure_payload_classifies_and_preserves`.
- Success rendering (api/console URLs normalized to `127.0.0.1`, docker,
  providers status-only, health, next steps): **Complete**.
  - `_start_success_payload` with `_normalize_local_url` / `_docker_summary` /
    `_providers_summary`; covered by `test_start_success_json_payload`,
    `test_start_success_pretty_panel`,
    `test_start_success_payload_normalizes_local_urls`, and
    `test_start_success_payload_preserves_remote_console_url`.
- Output routing and exit codes: JSON to stdout; pretty failure to stderr;
  success exit 0, failure exit 1, `KeyboardInterrupt` exit 130: **Complete**.
  - `_render_start_payload`; covered by `test_start_failure_pretty_goes_to_stderr`
    and `test_start_keyboard_interrupt_exits_130`.
- Secret redaction in start output: **Complete**.
  - First-run renderer redacts; provider summary is status-only. Covered by
    `test_start_redacts_tokens_in_failure_output`.
- No payload built at import time (lazy first-run wiring): **Complete**.
  - Covered by `tests/unit/cli/test_first_run_command_imports.py`.
- `awf start` help text updated (`src/awf/cli/main.py`); placeholder helper
  removed while keeping `AWF_START_PLACEHOLDER` in rendering/reasons: **Complete**.
- Explicitly-not-touched boundaries respected (`setup_commands.py`,
  `pyproject.toml`/package data, `smoke.py` runtime, docs-drift, `mcp/*`):
  **Complete** — see "Files touched" below; none of those files appear.

## Files Touched

- `plans/T05_START_WRAPPER_PLAN.md`, `plans/T05_START_WRAPPER_VALIDATION.md` (this file).
- `src/awf/cli/start_commands.py` — real `start_command` + pure helpers.
- `src/awf/cli/main.py` — `start` command help text.
- `src/awf/service/bootstrap.py` — `asset_root` param + `force_rebuild` flag.
- `tests/unit/cli/test_start_commands.py` (rewrite).
- `tests/unit/service/test_bootstrap_parts/test_bootstrap_part_003.py` (focused additions).

## Verification Commands (focused; AWF/CI own broad validation)

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/cli/start_commands.py src/awf/service/bootstrap.py \
  tests/unit/cli/test_start_commands.py
# All checks passed!

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/cli/start_commands.py src/awf/service/bootstrap.py \
  tests/unit/cli/test_start_commands.py
# 3 files already formatted

uv run --python 3.12 --extra dev mypy \
  src/awf/cli/start_commands.py src/awf/service/bootstrap.py
# Success: no issues found in 2 source files

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_start_commands.py -q
# 28 passed

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_first_run_command_imports.py -q
# 2 passed

uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap_parts -q
# 51 passed
```

Full-suite, whole-repo coverage (99% gate), and the OpenAPI drift gate are owned
by AWF + GitHub CI after the agent phase.

## Gaps / Residual Risk

- No `Partial`/`Missing` requirements remain; no follow-up iteration needed.
- This validation exercises the wrapper via patched bootstrap wiring (unit
  level). End-to-end proof that `awf start` boots a real local Core (Docker +
  Postgres + migrate + API) is owned by AWF/GitHub validation and the separate
  no-token smoke proof (T10), which is intentionally out of T05 scope.
