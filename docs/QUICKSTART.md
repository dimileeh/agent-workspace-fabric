# Quickstart

Get a local AWF control plane running and prove the operator path in a few
commands.

## Prerequisites

- Git.
- Docker Desktop or Docker Engine with the Compose plugin running.
- `uv`.
- GitHub CLI `gh` if you want AWF to create or monitor PRs.
- At least one coding-agent credential for real workspace execution.

## Install

For a released install:

```bash
uv tool install agent-workspace-fabric
```

`pipx` is the equivalent isolated install path if you prefer it:

```bash
pipx install agent-workspace-fabric
```

Use plain `pip` only inside an active virtualenv:

```bash
python -m venv .venv
. .venv/bin/activate
pip install agent-workspace-fabric
```

For contributor or source checkout work:

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force
```

Homebrew is planned after AWF has stable tagged Python artifacts and a passing
formula audit.

## Set Up And Start AWF

The recommended first-run sequence is setup, start, health check, then project
onboarding. Keep local runtime values in root `.env` so the CLI, worker, MCP
server, and raw Docker Compose lane all read the same configuration:

```bash
cp .env.example .env
awf setup
awf start
awf service status --format pretty
```

`awf setup` checks host readiness and imports any legacy `docker/compose/.env`
values into root `.env`. `awf start` starts the local AWF Core stack, and
`awf service status --format pretty` confirms API, database, Docker, image,
disk, provider, and cleanup health.

For source checkouts or raw Docker installs, root Compose can bring up the full
local stack with safe loopback-only defaults:

```bash
docker compose up --build
```

Open <http://127.0.0.1:3000> for the console, or call the API at
<http://127.0.0.1:8000>. Protected local API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

If you set or refresh the GitHub token after starting Core, rerun the service
start command so Compose recreates the service containers with the updated
environment:

```bash
awf start
```

## Open The Console

The raw Docker Compose path starts the console for you. When using
`awf setup` / `awf start`, or when developing the console itself, run it
manually:

```bash
npm --prefix apps/console run dev
```

Open <http://127.0.0.1:3000>. AWF uses a local console URL in smoke reports.

## Onboard A Project

From a checked-out project repository:

```bash
awf init .
awf profile preview . --format pretty
awf smoke run --mocked-local --format pretty
```

If `.awf/workspace.yml` already exists, `awf init .` validates local readiness
and points you at preview/smoke commands. If no profile exists, interactive
terminals guide you through a short setup. For automation, write detected
defaults without prompting:

```bash
awf init . --write-profile --yes
```

## When Something Fails

- Use `awf service doctor` for local prerequisite failures.
- Use `awf service status --format pretty` for local API, database, Docker,
  image, disk, provider, and cleanup health.
- Use `awf service readiness --format pretty` only as the AWF Core
  release-readiness gate; it includes historical PRD SLO evidence and may fail
  even when the local service is healthy.

Next:

- [Project Onboarding](PROJECT_ONBOARDING.md)
- [PR Monitor Adoption](PR_MONITOR_ADOPTION.md)
- [DX Smoke Command](SMOKE_COMMAND.md)
- [Troubleshooting](TROUBLESHOOTING.md)
