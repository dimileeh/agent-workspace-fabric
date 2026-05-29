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

The current runnable first-run sequence is local Core startup, health check,
then project onboarding. Export the required local service values before
starting Core so Compose can interpolate the API, worker, and Postgres service
environment:

```bash
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_GITHUB_TOKEN="$(gh auth token)"
awf service bootstrap
awf service status --format pretty
```

`awf service bootstrap` starts the local AWF Core stack, and
`awf service status --format pretty` confirms API, database, Docker, image,
disk, provider, and cleanup health.

`awf setup` and `awf start` are reserved first-run command surfaces. They are
present in help for the future grammar, but today `awf setup` exits with
`AWF_SETUP_PLACEHOLDER` and `awf start` exits with `AWF_START_PLACEHOLDER`; use
`awf service bootstrap` until those setup and start slices land.

In source checkouts with local Compose assets, `awf service bootstrap` reads
`docker/compose/.env` when that file already exists. If you prefer persistent
values across shells, copy `.env.example` to `docker/compose/.env` and set
`AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, and `AWF_GITHUB_TOKEN` there before
bootstrapping.

If you set or refresh the GitHub token after starting Core, rerun the service
bootstrap so Compose recreates the service containers with the updated
environment:

```bash
awf service bootstrap
```

## Open The Console

The local console runs separately from the service stack:

```bash
npm --prefix apps/console run dev
```

Open <http://localhost:3000>. AWF uses `localhost` as the default local console
host in smoke reports.

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
