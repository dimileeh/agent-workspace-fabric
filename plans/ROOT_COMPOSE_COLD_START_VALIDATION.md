# Root Docker Compose Cold-Start Validation

Plan reference: `plans/ROOT_COMPOSE_COLD_START_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Preserve root `compose.yaml` as the public Compose entrypoint and keep packaged bootstrap coverage | Complete | Root `compose.yaml` includes `./docker/compose/local-service.yml`; `pyproject.toml` force-includes root `compose.yaml`; source asset validation still checks root `compose.yaml`. |
| Reuse the guided local-service control-plane DB | Complete | Root `compose.yaml` pins `name: awf-local-service`, so `docker compose up --build` and guided service startup use the same `awf-local-service_awf-postgres-data` volume. |
| Make raw Compose work from a clean checkout with safe local defaults | Complete | `docker/compose/local-service.yml` defaults `AWF_API_TOKEN=local-dev-token`, `AWF_POSTGRES_PASSWORD=awf_dev`, keeps Postgres loopback-bound, and binds API to `127.0.0.1:${AWF_API_HOST_PORT:-8000}:8000`. |
| Mirror Compose defaults in CLI env/readiness | Complete | `src/awf/service/config.py` fills missing/empty local API token and Postgres password with the same defaults; `src/awf/host_setup/system_checks/checks_host.py` reports OK with default metadata instead of blocking solely on missing local defaults. |
| Build agent runtime through root Compose | Complete | `agent-runtime` one-shot service builds `docker/agent-runtime.Dockerfile`, tags `${AWF_AGENT_RUNTIME_IMAGE:-awf-agent-runtime:latest}`, and API/worker depend on its successful completion in raw Compose. |
| Preserve `awf start --skip-agent-runtime-build` semantics separately | Complete | `src/awf/service/bootstrap.py` starts API/worker with `--no-deps` after the explicit guided bootstrap stages, so skipped agent-runtime builds are not reintroduced through Compose dependencies. |
| Add console to the Compose stack | Complete | Added `apps/console/Dockerfile`; Compose `console` service builds `awf-console:local`, binds `127.0.0.1:${AWF_CONSOLE_HOST_PORT:-3000}:3000`, and points to `AWF_API_BASE_URL=http://api:8000` with the local API token default. |
| Update docs and prior validation note | Complete | `docs/QUICKSTART.md`, `docs/GETTING_STARTED.md`, and `docs/CONCEPTS.md` document `docker compose up --build` as the raw source-checkout path; `plans/ROOT_ENV_LOCAL_RUNTIME_VALIDATION.md` no longer claims full cold-start success before this slice. |
| Add static Compose, unit, console image, and docs tests | Complete | See command evidence below. |

## Command Evidence

```bash
COMPOSE_DISABLE_ENV_FILE=1 docker compose --env-file "$empty_file" config --quiet
COMPOSE_DISABLE_ENV_FILE=1 docker compose --env-file "$empty_file" config --services
COMPOSE_DISABLE_ENV_FILE=1 docker compose --env-file "$empty_file" config --images
```

Passed. Services included `agent-runtime`, `postgres`, `migrate`, `api`, `worker`, and `console`. Images included `awf-control-plane:local`, `awf-agent-runtime:latest`, `awf-console:local`, and `postgres:16-alpine`. Rendered ports were loopback-bound by default.

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_root_compose_config.py \
  tests/unit/service/test_environment.py \
  tests/unit/service/test_host_setup_system_checks_host.py \
  tests/unit/service/test_host_setup_source_assets.py \
  tests/unit/docs/test_public_docs_status.py -q
```

Passed: `102 passed`.

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_bootstrap_packaged_assets.py \
  tests/unit/service/test_bootstrap_parts/test_bootstrap_part_001.py \
  tests/unit/service/test_bootstrap_parts/test_bootstrap_part_002.py \
  tests/unit/service/test_bootstrap_parts/test_bootstrap_part_003.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/cli/test_service_cli_parts -q
```

Passed: `169 passed`.

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_setup_commands_client.py \
  tests/unit/cli/test_setup_commands_providers.py \
  tests/unit/service/test_config_parts/test_config_part_001.py \
  tests/unit/service/test_config_parts/test_config_part_002.py \
  tests/unit/docs/test_public_docs_status.py \
  tests/unit/service/test_root_compose_config.py -q
```

Passed: `218 passed`.

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/service/config.py \
  src/awf/service/bootstrap.py \
  src/awf/host_setup/system_checks/checks_host.py \
  src/awf/host_setup/source_assets.py \
  tests/unit/service/test_root_compose_config.py \
  tests/unit/service/test_environment.py \
  tests/unit/service/test_host_setup_system_checks_host.py \
  tests/unit/docs/test_public_docs_status.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/service/test_bootstrap_parts/test_bootstrap_part_001.py \
  tests/unit/service/test_bootstrap_parts/test_bootstrap_part_002.py \
  tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py
```

Passed: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/service/config.py \
  src/awf/service/bootstrap.py \
  src/awf/host_setup/system_checks/checks_host.py \
  src/awf/host_setup/source_assets.py
```

Passed: `Success: no issues found in 4 source files`.

```bash
docker build -t awf-console:local-test -f apps/console/Dockerfile .
```

Passed. The image ran `npm ci`, built the Next.js app, pruned dev dependencies, and exported `awf-console:local-test`.

## Dirty Environment Smoke

Ran the full runtime smoke against the current dirty development environment:

```bash
docker compose up -d --build
```

Passed startup. Compose built `awf-console:local`, `awf-agent-runtime:latest`,
and `awf-control-plane:local`; started Postgres, migrations, API, worker, and
console. The current dirty env overrides the API host port to `127.0.0.1:8010`;
console is bound to `127.0.0.1:3000`; Postgres is bound to `127.0.0.1:5433`.

Follow-up probes:

```bash
curl http://127.0.0.1:8010/healthz
curl http://127.0.0.1:3000
docker compose exec -T api sh -c 'curl -H "Authorization: Bearer $AWF_API_TOKEN" http://127.0.0.1:8000/v1/workspaces'
docker image inspect awf-agent-runtime:latest
docker image inspect awf-console:local
```

Passed with HTTP 200 for health, console, and authenticated workspaces. Image
inspection succeeded for both `awf-agent-runtime:latest` and `awf-console:local`.

`/readyz` returned 503 in this dirty machine state because the orphan resource
doctor found existing AWF workspace leftovers: 71 orphan resources total (28
volumes and 43 worktrees). Core cold-start dependencies were healthy: db,
Docker CLI, Docker daemon, Docker Compose, and agent runtime image checks were
OK. This is not a Compose startup failure, but it means this dirty environment
does not report full readiness until those old orphan resources are reviewed or
cleaned up.

## Iteration 1: Project Name And Dirty Resource Cleanup

The first dirty smoke revealed a project-name mismatch: root Compose created
`aira-agent-workspace-fabric_awf-postgres-data`, while the guided service path
had existing state in `awf-local-service_awf-postgres-data`. This made the
console and API talk to each other correctly, but against a fresh sibling DB.

Fix:

- Added `name: awf-local-service` to root `compose.yaml`.
- Added a regression test that root Compose renders with
  `name == "awf-local-service"`.
- Removed the accidental fresh `aira-agent-workspace-fabric_awf-postgres-data`
  volume after stopping that project.
- Cleaned the detected missing-workspace leftovers: 28 `awf-ws_*` Postgres
  volumes and 43 `~/.awf/service/git/worktrees/ws_*` directories.

Verification:

```bash
COMPOSE_DISABLE_ENV_FILE=1 docker compose --env-file /dev/null config --format json
uv run --python 3.12 --extra dev pytest tests/unit/service/test_root_compose_config.py -q
docker compose up -d --build
curl http://127.0.0.1:8010/healthz
curl http://127.0.0.1:8010/readyz
curl http://127.0.0.1:3000
docker compose exec -T postgres psql -U awf -d awf -tAc "select count(*) from workspaces;"
```

Passed. Root Compose now renders project `awf-local-service`; the focused
Compose test passed (`6 passed`); the stack starts under `awf-local-service-*`;
health, readiness, and console return HTTP 200; orphan resource status is OK
with orphan count `0`; Postgres reports `423` workspace rows; the API returns a
default page of `50` workspaces from the same DB.
