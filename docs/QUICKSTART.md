# Quickstart

Pick one lane and follow only that lane. Each available lane gets AWF
installed, runs the host setup check, starts local Core, initializes a project,
runs mocked smoke, and shows the matching upgrade and uninstall path.

Use the source checkout lanes when you want to inspect AWF before running it.
Use the `uv tool` / `pipx` lane when you want the published package. The hosted curl
installer lane is intentionally omitted until its public installer, manifest,
checksums, and distribution artifacts are published and verified.
In source checkout lanes, `awf setup` checks and `awf start` uses
`docker/compose/.env` for Compose-interpolated local service values.

## Prerequisites

- Git.
- Docker Desktop or Docker Engine with the Compose plugin running.
- `uv` for lanes that use `uv`, or `pipx` for the `pipx` lane.
- GitHub CLI `gh` if you want AWF to create or monitor PRs.
- At least one coding-agent credential for real workspace execution.

The mocked smoke command below does not require live GitHub or provider access.
Local first-run URLs use the smoke defaults: API checks use
`http://localhost:8000` by default, and the console is
`http://localhost:3000` when the console is running.

## Lane 1: uv tool or pipx

This lane is release-installed and package-manager mediated. `uv tool` and
`pipx` install the published `agent-workspace-fabric` package into an isolated
tool environment.

Install AWF with one package manager.

`uv tool`:

```bash
uv tool install agent-workspace-fabric
```

`pipx`:

```bash
pipx install agent-workspace-fabric
```

Then run the shared first-run commands:

```bash
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup
awf start

mkdir -p "$HOME/awf-eval-project"
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

This is the `awf init <path>` step. Supply any path, such as an empty eval
directory or a checked-out project.

Upgrade:

Run the upgrade command for the package manager you used to install AWF.

`uv tool`:

```bash
uv tool upgrade agent-workspace-fabric
```

`pipx`:

```bash
pipx upgrade agent-workspace-fabric
```

Then restart AWF and rerun smoke. If `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` are not already persisted in `.env`, restore them in
this shell before restarting. Restore the same `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` used by the running local Core; do not generate
replacement service secrets during upgrade:

```bash
if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
awf start
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Uninstall:

Run the uninstall command for the package manager you used to install AWF.

`uv tool`:

```bash
uv tool uninstall agent-workspace-fabric
```

`pipx`:

```bash
pipx uninstall agent-workspace-fabric
```

## Lane 2: Source Checkout With Global Tool Install

This lane uses inspectable source and then installs `awf` as a global tool from
that checkout. It is useful when you want to inspect or patch AWF but still want
the normal `awf` executable on `PATH`.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force

export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"

mkdir -p ../awf-eval-project
awf init ../awf-eval-project
awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before refreshing source-checkout metadata; `awf setup` checks the API and
Postgres host ports and blocks while the previous Core stack still holds them.

```bash
git pull
uv tool install . --force
if ! grep -q '^AWF_API_TOKEN=.' docker/compose/.env .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in docker/compose/.env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in docker/compose/.env .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^AWF_POSTGRES_PASSWORD=//p' "$env_file" | head -n 1)"
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in docker/compose/.env or .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

Uninstall:

Before uninstalling the global tool or deleting the checkout, make sure
`~/.awf/config.yml` no longer records it under `source_checkout`. Refreshing
through `awf setup --source-checkout ...` is not metadata-only. Stop local Core
before refreshing source-checkout metadata; `awf setup` checks the API and
Postgres host ports and blocks while the previous Core stack still holds them.
Editing `~/.awf/config.yml` remains the no-stop option. To refresh the persisted
path:

```bash
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

or edit `~/.awf/config.yml` and remove only the top-level `source_checkout:` block.
Keep provider, client, and consent entries unless you intentionally want to reset
host setup state.

```bash
uv tool uninstall agent-workspace-fabric
```

```bash
cd ..
rm -rf aira-agent-workspace-fabric
```

Only delete the AWF checkout if it was created just for evaluation and no
persisted `source_checkout` metadata points at it.

## Lane 3: Source Checkout With No Global Install

This lane uses inspectable source and no global install. It does not place an
`awf` executable on the global `PATH`; every AWF command runs through `uv run`
from the checkout.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv sync --extra dev

export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"

mkdir -p ../awf-eval-project
uv run --python 3.12 --extra dev awf init ../awf-eval-project
uv run --python 3.12 --extra dev awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before refreshing source-checkout metadata; `awf setup` checks the API and
Postgres host ports and blocks while the previous Core stack still holds them.

```bash
git pull
uv sync --extra dev
if ! grep -q '^AWF_API_TOKEN=.' docker/compose/.env .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in docker/compose/.env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in docker/compose/.env .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^AWF_POSTGRES_PASSWORD=//p' "$env_file" | head -n 1)"
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in docker/compose/.env or .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

Uninstall:

Before deleting the checkout, make sure `~/.awf/config.yml` no longer records it
under `source_checkout`. Refreshing through `awf setup --source-checkout ...` is
not metadata-only. Stop local Core before refreshing source-checkout metadata;
`awf setup` checks the API and Postgres host ports and blocks while the previous
Core stack still holds them. Editing `~/.awf/config.yml` remains the no-stop
option. To refresh the persisted path:

```bash
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

or edit `~/.awf/config.yml` and remove only the top-level `source_checkout:` block.
Keep provider, client, and consent entries unless you intentionally want to reset
host setup state.

```bash
cd ..
rm -rf aira-agent-workspace-fabric
```

Only delete the AWF checkout if it was created just for evaluation and no
persisted `source_checkout` metadata points at it.

## After Start

`awf start` prints the local API and console URLs. Use
`http://localhost:3000` for the console when it is running, and
`http://localhost:8000/readyz` for a direct local API readiness check.

Next:

- [Project Onboarding](PROJECT_ONBOARDING.md)
- [Upgrade Guide](UPGRADE.md)
- [Uninstall Guide](UNINSTALL.md)
- [DX Smoke Command](SMOKE_COMMAND.md)
- [Troubleshooting](TROUBLESHOOTING.md)
