# T05 — Add `awf start` Wrapper Over Existing Service Bootstrap (Plan)

Protocol: `plans/PLAN_EXECUTION_PROTOCOL.md` / `AGENTS.md`. This is the
task-specific implementation plan that complements the AWF planning artifact
`docs/awf-plans/ws_54c716f206e8484ca946e4fa.md`. Validation evidence lands in
`plans/T05_START_WRAPPER_VALIDATION.md` after implementation.

## Objective

Replace the reserved `awf start` placeholder with a real, friendly wrapper that
**delegates to the existing `run_service_bootstrap` engine** to start local AWF
Core, then renders a first-run-contract success panel or a reason-coded failure.
T05 owns only the start CLI delegation, the source/package asset-selection hooks
that start needs, and tests. It does **not** reimplement service startup, rework
`awf setup` (T04), implement package-data inclusion (T13), or add the no-token
smoke proof (T10).

Dependencies T01/T02/T03 are merged on `development` and treated as available:
the registered `awf start` command surface, the host-setup config +
source-checkout asset model, and the first-run error contract/rendering helpers +
`START_*` reason catalog.

## Public interface

```
awf start [--rebuild] [--skip-agent-runtime-build] [--timeout-seconds N]
          [--source-checkout PATH] [--format json|pretty]
```

- `--format json|pretty` — `OutputFormat`, default `pretty`.
- `--timeout-seconds N` — `float`, `min=0.0`, default `180.0`; maps to
  `ServiceBootstrapOptions.timeout_seconds`.
- `--skip-agent-runtime-build` — maps to
  `ServiceBootstrapOptions.skip_agent_runtime_build=True`.
- `--rebuild` — forces a from-scratch agent-runtime build via a new, scoped
  `ServiceBootstrapOptions.force_rebuild` flag (`docker build --no-cache ...`).
  **Mutually exclusive** with `--skip-agent-runtime-build`; passing both exits
  with code 2 and never attempts a Core start.
- `--source-checkout PATH` — optional path selecting verified source-checkout
  assets explicitly.

The command reuses the existing `service_bootstrap` wiring
(`_resolve_service_compose_paths()`, `_resolve_service_runtime_env_files()`,
`local_service_environ()`, `resolve_service_settings()`), builds
`ServiceBootstrapOptions`, calls `run_service_bootstrap(...)`, and translates the
structured result/exception into first-run output. Start passes no strict
providers.

## Asset selection (precedence, before any Core start)

1. **Explicit `--source-checkout PATH`** — `validate_source_checkout(path)`. On
   `SourceCheckoutError`, render a reason-coded first-run failure
   (`SOURCE_CHECKOUT_INVALID` / `SOURCE_CHECKOUT_ASSETS_STALE`) carrying
   `missing_markers`/details, exit 1, no Core start. On success pin bootstrap to
   the verified checkout root + compose file.
2. **Stored config metadata** — when no `--source-checkout`, attempt
   `read_host_setup_config()`; if `config.source_checkout` is present, revalidate
   via `verified_source_from_metadata(...)`. A stale/moved checkout raises
   `SOURCE_CHECKOUT_ASSETS_STALE` and fails loudly (no silent fallback). A
   missing config, a config with no source-checkout metadata, or an
   unreadable/corrupt config (when source assets were not requested) falls
   through to (3).
3. **Default discovery** — no explicit selection; `run_service_bootstrap`
   discovers assets exactly as today (`asset_root=None`).

When (1) or (2) yields a `VerifiedSourceCheckout`, start passes
`compose_file=verified.compose_file` **and** the new `asset_root=verified.root`
hook so build/compose/env-file resolution all use the selected checkout.

## Bootstrap hooks (`src/awf/service/bootstrap.py`)

Two minimal, backward-compatible additions (defaults preserve current behavior):

- `asset_root: Path | None = None` on `run_service_bootstrap(...)`, threaded into
  `_resolve_bootstrap_assets(compose_file, *, require_agent_runtime,
  asset_root=None)`. When provided, asset resolution is **pinned** to that root
  (compose, agent-runtime Dockerfile, compose env file derived from it) instead
  of running `_resolve_bootstrap_asset_root()` discovery. An invalid
  `asset_root` raises `_bootstrap_assets_not_found_error(...)`
  (`SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND`).
- `force_rebuild: bool = False` on `ServiceBootstrapOptions`. When `True`, the
  `agent_runtime_build` stage command gains `--no-cache`. No effect when
  `skip_agent_runtime_build=True`.

## Failure translation (preserve structured bootstrap failures)

Pure helper `_start_failure_payload(exc) -> FirstRunPayload` classifies the
structured bootstrap error into a `START_*` reason code while embedding the full
`exc.to_dict()` diagnostic under issue `details["bootstrap"]`:

- `SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND` → `START_COMPOSE_ASSETS_MISSING`.
- `SERVICE_BOOTSTRAP_TIMEOUT` → `START_HEALTH_TIMEOUT` (carries `last_status`).
- `SERVICE_BOOTSTRAP_STAGE_FAILED` with `stage == "migrate"` →
  `START_MIGRATION_FAILED`.
- `SERVICE_BOOTSTRAP_STAGE_FAILED` whose stdout/stderr matches a Docker
  port-bind signature → `START_PORT_CONFLICT`.
- Any other failure → a payload built manually that echoes the original
  `exc.reason_code` and still embeds the full diagnostic (no dropped failures).

Failure output: JSON → stdout via `_emit`; pretty → stderr via
`render_first_run_pretty`; exit 1. `KeyboardInterrupt` keeps exit 130.

## Success rendering

Pure helper `_start_success_payload(settings, result) -> FirstRunPayload`
building `first_run_success_payload(command="awf start", ...)` with details:
`api_url` (host normalized to `127.0.0.1`), `console_url`
(`settings.console_url` or `smoke.DEFAULT_LOCAL_CONSOLE_URL`, host `127.0.0.1`),
`docker` (from `result.service_status["checks"]["docker"]`), `providers` (from
`result.service_status["agent_readiness"]`), `health`
(`result.service_status["status"]`). `next_steps` point to `awf init <path>` plus
`awf service status` / the console URL. Success: stdout, exit 0.

## Files touched

- `plans/T05_START_WRAPPER_PLAN.md` (this file), `plans/T05_START_WRAPPER_VALIDATION.md`.
- `src/awf/cli/start_commands.py` — real `start_command` + pure helpers; remove
  the placeholder helper (keep `AWF_START_PLACEHOLDER` in rendering/reasons).
- `src/awf/cli/main.py` — update `start` command help text only.
- `src/awf/service/bootstrap.py` — `asset_root` param + `force_rebuild` flag.
- `tests/unit/cli/test_start_commands.py` (rewrite), `tests/unit/service/test_bootstrap_parts/` (focused additions).

Explicitly not touched: `setup_commands.py` (T04), `pyproject.toml`/package data
(T13), `smoke.py` runtime (T10), docs/docs-drift (T15/T18), `mcp/*` (T09).

## TDD order

1. Add focused bootstrap tests for `asset_root` pinning + `force_rebuild`
   `--no-cache`; implement the two hooks.
2. Rewrite `tests/unit/cli/test_start_commands.py` (parser/help/dispatch,
   delegation, source asset selection, structured failure preservation,
   redaction, pure-helper unit tests); implement `start_command` + helpers.
3. Keep `tests/unit/cli/test_first_run_command_imports.py` green (no payload at
   import time).

## Validation (focused; AWF/CI own broad validation)

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_start_commands.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_first_run_command_imports.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap_parts -q
```

Full-suite, whole-repo coverage, and the OpenAPI drift gate are owned by AWF +
GitHub CI after the agent phase.
