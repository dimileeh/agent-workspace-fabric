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

For contributor or source checkout work:

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install .
```

## Bootstrap AWF

For PR creation and monitoring, export a GitHub token before bootstrapping so
the API and worker containers receive it when they are created:

```bash
export AWF_GITHUB_TOKEN="$(gh auth token)"
awf init
awf service status --format pretty
```

`awf init` without a path checks local prerequisites, creates the host state
directory, writes `docker/compose/.env` from `.env.example` when the source
checkout contains `docker/compose/local-service.yml` (or `.env` in package
installs), and starts the local Postgres, migration, API, and worker stack.

If you set or refresh the GitHub token after `awf init`, rerun the service
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
