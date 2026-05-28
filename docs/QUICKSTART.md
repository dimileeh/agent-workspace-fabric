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

The public first-run sequence is machine setup, local Core startup, then
project onboarding. For PR creation and monitoring, export a GitHub token
before starting Core so the API and worker containers receive it when they are
created:

```bash
export AWF_GITHUB_TOKEN="$(gh auth token)"
awf setup
awf start
awf service status --format pretty
```

`awf setup` prepares the machine for AWF, `awf start` starts the local AWF Core
stack, and `awf service status --format pretty` confirms API, database, Docker,
image, disk, provider, and cleanup health.

In source checkouts with local Compose assets, `awf start` and
`awf service bootstrap` persist Compose-interpolated service values in
`docker/compose/.env`.

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
