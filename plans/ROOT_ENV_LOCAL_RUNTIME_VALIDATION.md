# Root Env Local Runtime Validation

## Result

Implemented the root `.env` local runtime configuration plan.

AWF now treats the install/source root `.env` as the canonical operator-edited
runtime env file. The root `compose.yaml` is the public raw Docker Compose
entrypoint and includes `docker/compose/local-service.yml` as an internal asset.
Setup/start/bootstrap migrate legacy `docker/compose/.env` values into root
`.env` without printing values, then back up the legacy file. Read-only service
commands resolve root `.env` and ignore legacy nested compose env files unless a
future explicit migration entrypoint handles them.

## Plan Checklist

- Root `compose.yaml` supports repo-root Compose resolution for the existing
  local service stack; the full cold-checkout `docker compose up --build` proof
  is covered by the follow-up root Compose cold-start slice.
- Source-checkout and packaged helpers resolve root `compose.yaml` and root `.env`.
- `awf setup`, `awf start`, `awf service bootstrap/status/doctor/logs/gc`, and
  MCP env registration target root `.env`.
- Legacy `docker/compose/.env` is migration-only and is no longer an active
  operator-edited config surface.
- Migration creates missing root `.env` from `.env.example` plus legacy values,
  imports only missing legacy keys into existing root `.env`, keeps root values
  canonical on conflicts, and reports key names only.
- Legacy files are preserved as timestamped backups.
- Migration payloads and tests avoid raw secret values.
- Process env precedence remains intact.
- Compose interpolation helpers follow included Compose files.
- Docs and installer backlog use root `.env` and repo-root raw Compose as the
  supported public flow.

## Validation Commands

- `docker compose config --quiet`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py tests/unit/service/test_host_setup_source_assets.py tests/unit/service/test_bootstrap_packaged_assets.py tests/unit/service/test_bootstrap_parts/test_bootstrap_part_001.py tests/unit/service/test_bootstrap_parts/test_bootstrap_part_002.py tests/unit/service/test_bootstrap_parts/test_bootstrap_part_003.py tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_002.py tests/unit/service/test_host_setup_system_checks_host.py tests/unit/service/test_host_setup_system_checks_probes.py tests/unit/cli/test_init_parts tests/unit/cli/test_init_ops_coverage_helpers.py tests/unit/cli/test_service_cli_parts tests/unit/cli/test_setup_commands.py tests/unit/cli/test_setup_commands_client.py tests/unit/cli/test_setup_commands_providers.py tests/unit/cli/test_start_commands.py tests/unit/cli/test_service_gc_cli.py tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_api_surface_cleanup_docs.py tests/unit/docs/test_sdk_stance_docs.py -q`
  - Passed: 677 tests.
- `uv run --python 3.12 --extra dev ruff check <touched source and test files>`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/env_migration.py src/awf/service/config.py src/awf/host_setup/source_assets.py src/awf/service/bootstrap.py src/awf/cli/init_ops.py src/awf/cli/service_commands.py src/awf/cli/start_commands.py src/awf/cli/setup_commands.py src/awf/cli/mcp_commands.py src/awf/service/environment.py src/awf/host_setup/system_checks/checks_host.py src/awf/service/provider_readiness.py src/awf/host_setup/system_checks/primitives.py src/awf/host_setup/system_checks/checks_ports.py`
  - Passed: no issues in 14 source files.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed: `openapi.json` matches the current app spec.
- `uv run --python 3.12 --extra dev pytest -n 20 --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99`
  - Initial run: failed 39 tests, while coverage itself reached 99.08%.
  - Follow-up fixes aligned stale tests and the API port probe with the root
    Compose cold-start contract.
  - Final run passed: 10675 passed, 1 skipped, total coverage 99.12%.
- `rg -n "docker/compose/\.env|docker compose --env-file docker/compose/\.env|-f docker/compose/local-service\.yml|AWF_SETUP_PLACEHOLDER|AWF_START_PLACEHOLDER" README.md docs TODO/awf-full-installer-first-run-setup-backlog.md -g '!docs/awf-plans/**'`
  - Passed with only legacy migration-source mentions and historical reason-code
    catalog entries remaining.

## Follow-up Adjustment

The first validation proved the root `.env` runtime migration and root
`compose.yaml` entrypoint, but it did not yet prove the full cold-checkout
Docker path requested for source evaluators. The follow-up
`ROOT_COMPOSE_COLD_START_PLAN.md` completes that gap by making raw
`docker compose up --build` build `awf-agent-runtime:latest`, start the console,
and use loopback-only local defaults when root `.env` is absent.

## Gaps

No known local validation gaps. GitHub/AWF CI remains the remote source of truth
for landing.
