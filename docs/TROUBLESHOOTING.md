# Troubleshooting

## First run troubleshooting

Use this guide for local Core first-run issues after installing AWF.

## Symptom: service bootstrap command fails

Run these checks after `awf init` or `awf service bootstrap` exits with an error:

1. Run the bootstrap and collect the full error output:

```bash
awf init
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
docker compose -f docker/compose/local-service.yml down --remove-orphans
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

4. Re-run an onboarding command once bootstrap succeeds:

```bash
awf init <path>
```

## Symptom: Provider auth/preflight failure

Use this when `awf service status` shows provider failures or warns for required
credentials:

```bash
awf service status --provider codex --format pretty
awf service status --provider claude_code --format pretty
awf service status --provider gemini --format pretty
awf service status --provider opencode --format pretty
```

Verify the configured provider auth surface:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `OLLAMA_API_KEY`
- local provider auth mounts under expected auth mount paths

Then rerun preflight and a minimal bootstrap check:

```bash
awf service bootstrap
awf service status --provider codex --format pretty
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

Use provider filtering for a failing dependency:

```bash
curl "http://localhost:8000/readyz?provider=codex"
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

- `awf service logs`
- `awf service logs --service api`
- `awf service logs --service worker`
- `awf service logs --service migrate`
- `awf service logs --service postgres`

Workspace-level logs:

- `awf workspace logs <workspace_id>`
- `awf workspace log <workspace_id> <stream_id>`
- `awf workspace events <workspace_id>`

API evidence endpoints:

- `GET /v1/workspaces/{workspace_id}/logs`
- `GET /v1/workspaces/{workspace_id}/events`
- `GET /v1/workspaces/{workspace_id}/operations`
- `GET /v1/workspaces/{workspace_id}/runtime`
- `awf workspace operations <workspace_id>`
- `awf workspace runtime <workspace_id>`

API equivalents for operators using REST:

- `GET /v1/workspaces/{workspace_id}/logs`
- `GET /v1/workspaces/{workspace_id}/logs/{stream_id}`
- `GET /v1/workspaces/{workspace_id}/events`
- `GET /v1/workspaces/{workspace_id}/operations`
- `GET /v1/workspaces/{workspace_id}/runtime`
- `GET /v1/workspaces/{workspace_id}`
