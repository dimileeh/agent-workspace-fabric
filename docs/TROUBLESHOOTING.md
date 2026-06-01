# Troubleshooting

## First-run troubleshooting

Use this guide for local Core first-run issues after installing AWF.

## Failure handling

AWF workspaces emit high-level `failure_reason` codes in:

- `awf workspace show <workspace_id> --format pretty`
- `awf workspace events <workspace_id>`

Core reason codes include:

- `agent_failure`
- `validation_failure`
- `infrastructure_failure`
- `policy_failure`
- `cleanup_failure`
- `profile_resolution_failure`
- `service_startup_failure`
- `phase_timeout`
- `health_check_failure`

When a workspace fails, it is preserved for operator inspection by default (logs,
runtime state, worktree, and artifacts). For cleanup, use explicit GC workflows
(`awf service gc`); triage evidence is kept outside workspace filesystem cleanup.

## Symptom: service bootstrap command fails

Run these checks after the lower-level `awf service bootstrap` command exits
with an error:

If `awf service bootstrap` reports `SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND`, run the
bootstrap command from an AWF source checkout. The local service bootstrap needs
`docker/compose/local-service.yml` and `docker/agent-runtime.Dockerfile`; an
installed package that does not explicitly bundle those assets cannot start the
local Docker stack by itself.

1. Run the bootstrap and collect the full error output:

```bash
awf service bootstrap
```

2. Confirm the service controller can report state:

```bash
awf service status --format pretty
```

3. Verify Docker and compose are visible to the current user:

```bash
docker --version
docker info
docker compose version
```

4. If there is an older stack in another profile, stop the overlapping project
   and restart:

```bash
docker compose ls
```

If compose still reports old local-service containers (`awf-local-service`),
remove only that stack before rerunning `awf service bootstrap`:

```bash
# If you are running from an AWF source checkout:
export AWF_LOCAL_SERVICE_PROJECT="awf-local-service"
docker compose -p "${AWF_LOCAL_SERVICE_PROJECT}" -f docker/compose/local-service.yml down --remove-orphans

# If you are using an installed `awf` package outside the source tree:
export AWF_LOCAL_SERVICE_PROJECT="awf-local-service"
for container_id in $(docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --quiet); do
  docker rm -f "${container_id}"
done
for network_id in $(docker network ls --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --quiet); do
  docker network rm "${network_id}"
done
for volume_name in $(docker volume ls --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --quiet); do
  case "${volume_name}" in
    *awf-postgres-data)
      echo "Skipping control-plane Postgres volume ${volume_name} (preserve)"
      ;;
    *)
      docker volume rm -f "${volume_name}"
      ;;
  esac
done
```

If stale workspace stacks remain, remove them by deterministic project name:

```bash
export WORKSPACE_ID=<workspace_id>
export AWF_COMPOSE_PROJECT="awf_${WORKSPACE_ID}"
docker ps -a \
  --filter "label=com.docker.compose.project=${AWF_COMPOSE_PROJECT}" \
  --format "table {{.ID}}\t{{.Names}}"
```

Then remove just that workspace project if the rendered compose file still exists:

```bash
export AWF_WORK_DIR="${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"
docker compose -f "${AWF_WORK_DIR}/compose/${WORKSPACE_ID}/compose.yml" \
  -p "${AWF_COMPOSE_PROJECT}" down --remove-orphans
```

If the compose file is missing, clean the same workspace resources via labels:

```bash
for container_id in $(docker ps -a --filter "label=com.docker.compose.project=${AWF_COMPOSE_PROJECT}" --quiet); do
  docker rm -f "${container_id}"
done
for network_id in $(docker network ls --filter "label=com.docker.compose.project=${AWF_COMPOSE_PROJECT}" --quiet); do
  docker network rm "${network_id}"
done
for volume_name in $(docker volume ls --filter "label=com.docker.compose.project=${AWF_COMPOSE_PROJECT}" --quiet); do
  docker volume rm -f "${volume_name}"
done
```

## Symptom: Postgres is unavailable, is recovering, or disk is full

AWF reports these as service status readiness problems; treat them as infra issues
before provider-level triage:

If the API host port was customized with `AWF_API_HOST_PORT`, host `awf`
workspace commands derive the matching localhost URL automatically when that
variable is in the same shell. Use `AWF_BASE_URL` only when the CLI or manual
curl requests run from another shell or through a reverse proxy:

```bash
export AWF_API_HOST_PORT=9001
export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"
awf workspace list --format pretty
curl "${AWF_BASE_URL}/readyz?provider=github"
```

`AWF_API_BASE_URL` remains the service-side API self-reference URL used by
doctor, smoke, and status flows; local Compose sets it inside the service
container to `http://api:8000`.

1. Check readiness signals and status detail:

```bash
awf service status --format pretty
awf service doctor
```

2. Confirm local stack container state:

```bash
docker ps
```

3. Check host resource pressure if you see `INSUFFICIENT_DISK`, `PORT_CLOSED`, or
   transient connectivity flaps:

```bash
docker system df
```

4. Free space cautiously, then rebuild:

```bash
docker image prune
awf service bootstrap
```

For local disk pressure, prefer deleting temporary artifacts over pruning mounted
volumes first so workspace recovery can reuse required compose workspaces.

## Symptom: Docker daemon/socket/Compose unavailable

If bootstrap or status checks cannot contact Docker:

1. Validate CLI/socket connectivity:

```bash
docker --version
docker info
docker compose version
```

2. Fix permissions for the current user and daemon socket:

```bash
ls -l /var/run/docker.sock
id -nG
id
sudo usermod -aG docker "$USER"
```

Apply the new group membership in your current session, then verify with:

```bash
newgrp docker
docker run --rm hello-world
```

On macOS and some shells, `newgrp` is unavailable; log out and log back in after
group update, then run:

```bash
docker info
```

3. If permission is denied or socket is missing, restart Docker and rerun status:

```bash
awf service status --format pretty
```

`DOCKER_DAEMON_UNREACHABLE` and `DOCKER_SOCKET_UNREACHABLE` indicate local host
integration problems; provider checks will usually recover once daemon/socket access
is restored.

## Symptom: Agent runtime image is missing

When workspace startup shows runtime image failures:

1. Check whether the runtime image exists:

```bash
docker image inspect awf-agent-runtime:latest
```

2. Rebuild the local service stack so the image is rebuilt or pulled:

```bash
awf service bootstrap
```

3. Confirm compose can see the runtime image from service commands:

```bash
awf service status --format pretty
```

4. Re-run project onboarding once bootstrap succeeds:

```bash
awf init <path>
```

## Symptom: Provider auth/preflight failure

Use this when `awf service status` shows provider failures or warns for required
credentials:

```bash
awf service status --provider codex --format pretty
awf service status --provider claude_code --format pretty
awf service status --provider cursor --format pretty
awf service status --provider gemini --format pretty
awf service status --provider opencode --format pretty
```

Verify the configured provider auth surface:

- `codex`: `OPENAI_API_KEY` (or `OPENAI_API_TOKEN`, `CODEX_API_KEY`, `CODEX_AUTH_TOKEN`)
- `claude_code`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`
- `cursor`: `CURSOR_API_KEY`
- `gemini`: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_ACCESS_TOKEN`, or `GOOGLE_APPLICATION_CREDENTIALS`
- `opencode`: `OLLAMA_API_KEY` (OpenCode’s Ollama auth surface) plus local auth at
  `~/.config/opencode` or `~/.ollama`
- Local provider auth mounts are copied into per-workspace directories and injected at runtime:
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/codex` → `/home/agent/.codex`
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/claude/.claude` and `.../auth/<workspace>/claude/.claude.json` → `/home/agent/.claude` and `/home/agent/.claude.json`
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/gemini/.gemini` → `/home/agent/.gemini`
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/opencode/.config/opencode` → `/home/agent/.config/opencode`
- Cursor uses env-only `CURSOR_API_KEY`; there is no `~/.cursor` auth mount.
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/ollama/.ollama` → `/home/agent/.ollama`

Then rerun preflight and a minimal bootstrap check:

```bash
awf service bootstrap
awf service status --format pretty
```

## Symptom: GitHub auth failure

AWF needs GitHub auth for PR and monitor behavior.

1. Confirm GitHub CLI auth:

```bash
gh auth status
```

2. Refresh one of:

```bash
export AWF_GITHUB_TOKEN="$(gh auth token)"
export GH_TOKEN="$(gh auth token)"
export GITHUB_TOKEN="$(gh auth token)"
```

3. Recreate service containers so the refreshed token is injected:

```bash
awf service bootstrap
```

4. Confirm service status and workspace operations recover after token update.

```bash
awf service status --format pretty
awf init <path>
```

## Symptom: `/readyz` reports warnings or 503

Readiness probes are your primary first-run signal.

```bash
curl http://localhost:8000/readyz
```

If `AWF_API_HOST_PORT` is customized, host CLI calls derive
`http://localhost:<port>` automatically when that variable is in the same shell.
Set `AWF_BASE_URL` for host-side API diagnostics that run from a different shell
or through a reverse proxy:

```bash
export AWF_API_HOST_PORT=9001
export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"
awf workspace list --format pretty
curl "${AWF_BASE_URL}/readyz?provider=github"
```

Use provider filtering for a failing dependency:

```bash
curl "http://localhost:8000/readyz?provider=codex"
curl "http://localhost:8000/readyz?provider=cursor"
curl "http://localhost:8000/readyz?provider=docker"
curl "http://localhost:8000/readyz?provider=github"
```

Map the result:

- Warnings indicate non-blocking optional provider risk; bootstrap can often proceed
  while you fix the warning source.
- `503` usually indicates a required readiness check is failing for this run.

Fix the named surface (`service`, `provider`, or `docker`) and rerun:

```bash
awf service status --format pretty
awf service bootstrap
```

## Symptom: Workspace is stuck or repeatedly fails

1. Inspect the workspace record:

```bash
awf workspace show <workspace_id> --format pretty
```

2. Check event timeline:

```bash
awf workspace events <workspace_id> --limit 50
```

3. Inspect runtime-level metadata:

```bash
awf workspace operations <workspace_id>
awf workspace runtime <workspace_id>
```

4. Review logs, including stream-level output:

```bash
awf workspace logs <workspace_id>
awf workspace log <workspace_id> agent.stdout
```

5. Interpret quick states:

- `failed`: check terminal logs and events, then retry the action that requeues work.
- `stuck`: check bootstrap dependencies (`readyz`, services, provider preflight), then
  rerun the failed step.
- `stale`: confirm controller has active monitoring and that no monitor policy timeout
  is expected.
- `cancelled`/`completed`: preserve artifact evidence for postmortem before cleanup.

## Where to find logs and evidence

Service-level logs:

- `awf service logs` (run from an AWF source checkout)
- `awf service logs --service api`
- `awf service logs --service worker`
- `awf service logs --service migrate`
- `awf service logs --service postgres`

If you installed `awf` outside the source tree, inspect those containers directly:

```bash
export AWF_LOCAL_SERVICE_PROJECT="awf-local-service"
docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --format "{{.ID}}\t{{.Names}}\t{{.Status}}"
api_id="$(docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --filter "label=com.docker.compose.service=api" --format "{{.ID}}" | head -n 1)"
[ -n "${api_id}" ] && docker logs -f "${api_id}" || echo "api container not found"
worker_id="$(docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --filter "label=com.docker.compose.service=worker" --format "{{.ID}}" | head -n 1)"
[ -n "${worker_id}" ] && docker logs -f "${worker_id}" || echo "worker container not found"
migrate_id="$(docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --filter "label=com.docker.compose.service=migrate" --format "{{.ID}}" | head -n 1)"
[ -n "${migrate_id}" ] && docker logs -f "${migrate_id}" || echo "migrate container not found"
postgres_id="$(docker ps -a --filter "label=com.docker.compose.project=${AWF_LOCAL_SERVICE_PROJECT}" --filter "label=com.docker.compose.service=postgres" --format "{{.ID}}" | head -n 1)"
[ -n "${postgres_id}" ] && docker logs -f "${postgres_id}" || echo "postgres container not found"
```

Workspace-level logs:

- `awf workspace logs <workspace_id>`
- `awf workspace log <workspace_id> <stream_id>`
- `awf workspace events <workspace_id>`
- `awf workspace operations <workspace_id>`
- `awf workspace runtime <workspace_id>`

REST API equivalents:

- `GET /v1/workspaces/{workspace_id}/logs/{stream_id}`
- `GET /v1/workspaces/{workspace_id}/logs`
- `GET /v1/workspaces/{workspace_id}/events`
- `GET /v1/workspaces/{workspace_id}/operations`
- `GET /v1/workspaces/{workspace_id}/runtime`
- `GET /v1/workspaces/{workspace_id}`
