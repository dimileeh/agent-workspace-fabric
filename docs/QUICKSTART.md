# Quickstart

Pick one lane and follow only that lane. Each available lane gets AWF
installed, runs the host setup check, starts local Core, initializes a project,
runs mocked smoke, and shows the matching upgrade and uninstall path.

Use the source checkout lanes when you want to inspect AWF before running it.
Use the `uv tool` / `pipx` lane when you want the published package. The hosted curl
installer lane is intentionally omitted until its public installer, manifest,
checksums, and distribution artifacts are published and verified.
All lanes use root `.env` for local runtime values. Existing legacy
`docker/compose/.env` files are migration sources only.

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

For source checkouts or raw Docker installs, root Compose can bring up the full
local stack with safe loopback-only defaults:

```bash
docker compose up --build
```

Open <http://localhost:3000> for the console, or call the API at
<http://localhost:8000>. Protected local API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

If you set or refresh the GitHub token after starting Core, rerun the start
command for the lane you used so Compose recreates the service containers with
the updated environment.

For Lane 1 (`uv tool` or `pipx`):

```bash
awf start
```

For Lane 2 (source checkout with global tool install), run from the checkout:

```bash
awf start --source-checkout "$PWD"
```

For Lane 3 (source checkout with no global install), run from the checkout:

```bash
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
```

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

Then run the shared first-run commands from the directory where AWF should keep
the package-lane `.env`. Persist the generated local service values before
setup/start so a later upgrade can restore the same running Core token and
password, and so host-side database checks use that same password:

```bash
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"
export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"
awf_env_tmp="$(mktemp)"
{
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_POSTGRES_PASSWORD=%s\n' "$AWF_POSTGRES_PASSWORD"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_DATABASE_URL=%s\n' "$AWF_DATABASE_URL"
  if [ -f .env ]; then
    sed \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_HOST_PORT[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=/d' \
      .env
  fi
} > "$awf_env_tmp"
mv "$awf_env_tmp" .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup
awf start
awf service status --format pretty

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
if ! grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]' .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
if ! grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*[^[:space:]]' .env 2>/dev/null; then
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

Keep local runtime values in the checkout-root `.env` so a later upgrade can
restore the same running Core token and password, and so host-side database
checks use that same password.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force
cp .env.example .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty

mkdir -p ../awf-eval-project
awf init ../awf-eval-project
awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before pulling new source files or refreshing source-checkout metadata; setup
checks the API and Postgres host ports and blocks while the previous Core stack
still holds them.

```bash
AWF_PERSISTED_API_TOKEN=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\") AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}" ;;
    \'*\') AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\") AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}" ;;
    \'*\') AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
docker compose --env-file .env -f docker/compose/local-service.yml stop
git pull
uv tool install . --force
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
AWF_PERSISTED_API_TOKEN=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\") AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}" ;;
    \'*\') AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\") AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}" ;;
    \'*\') AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
docker compose --env-file .env -f docker/compose/local-service.yml stop
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

Keep local runtime values in the checkout-root `.env` so a later upgrade can
restore the same running Core token and password, and so host-side database
checks use that same password.

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv sync --extra dev

cp .env.example .env
# [optional] Only needed for PR creation/monitoring; skip for mocked smoke.
# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed.
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty

mkdir -p ../awf-eval-project
uv run --python 3.12 --extra dev awf init ../awf-eval-project
uv run --python 3.12 --extra dev awf smoke run --project ../awf-eval-project --mocked-local --format pretty
```

This is the `awf init <path>` step for a checked-out project repository.

Upgrade:

Run this from the existing `aira-agent-workspace-fabric` checkout. If your shell
is elsewhere, first `cd /path/to/aira-agent-workspace-fabric`. Stop local Core
before pulling new source files or refreshing source-checkout metadata; setup
checks the API and Postgres host ports and blocks while the previous Core stack
still holds them.

```bash
AWF_PERSISTED_API_TOKEN=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\") AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}" ;;
    \'*\') AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\") AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}" ;;
    \'*\') AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before upgrading}"
  export AWF_POSTGRES_PASSWORD
fi
docker compose --env-file .env -f docker/compose/local-service.yml stop
git pull
uv sync --extra dev
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
AWF_PERSISTED_API_TOKEN=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_API_TOKEN="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_API_TOKEN" in
    \"*\") AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\"}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\"}" ;;
    \'*\') AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\'}"; AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break
done
if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then
  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env; do
  [ -f "$env_file" ] || continue
  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' "$env_file" | head -n 1)"
  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in
    \"*\") AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\"}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\"}" ;;
    \'*\') AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\'}"; AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\'}" ;;
  esac
  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break
done
if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then
  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"
else
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
docker compose --env-file .env -f docker/compose/local-service.yml stop
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
