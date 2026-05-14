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
uv tool install aira-awf
```

For contributor or source checkout work:

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install .
```

## Bootstrap AWF

```bash
awf init
awf service status --format pretty
```

`awf init` without a path checks local prerequisites, creates the host state
directory, writes `.env` from `.env.example` when needed, and starts the local
Postgres, migration, API, and worker stack.

For PR creation and monitoring, export a GitHub token visible to the worker:

```bash
export AWF_GITHUB_TOKEN="$(gh auth token)"
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
and points you at preview/smoke commands. If no profile exists, create one with:

```bash
awf profile init . --write
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
- [DX Smoke Command](SMOKE_COMMAND.md)
- [Troubleshooting](TROUBLESHOOTING.md)
