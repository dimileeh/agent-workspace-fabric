# Uninstall Guide

Use the uninstall path that matches the first-run lane or install path you
chose. Uninstalling the CLI or source checkout does not delete local AWF service state,
workspace state, Docker volumes, logs, or artifacts. The hosted curl installer
lane is intentionally omitted until its public installer, manifest, checksums,
and distribution artifacts are published and verified.

For destructive cleanup, use the targeted steps in
[Troubleshooting](TROUBLESHOOTING.md) only when you intentionally want to remove
local state.

If you ran `awf setup --source-checkout`, AWF records that checkout in
`~/.awf/config.yml` under `source_checkout`. Later `awf start` without an
explicit `--source-checkout` revalidates that path and fails if the directory is
gone. Refreshing through `awf setup --source-checkout ...` is not metadata-only.
Stop local Core before refreshing source-checkout metadata; `awf setup` checks
the API and Postgres host ports and blocks while the previous Core stack still
holds them. Editing `~/.awf/config.yml` remains the no-stop option. Before
deleting a recorded checkout, either refresh the persisted path:

```bash
cd /path/to/aira-agent-workspace-fabric
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
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
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
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
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

or edit `~/.awf/config.yml` and remove only the top-level `source_checkout:` block
to return to packaged or global install assets. Keep provider, client, and
consent entries unless you intentionally want to reset host setup state.

## uv tool

The `uv tool` lane is release-installed and package-manager mediated:

```bash
uv tool uninstall agent-workspace-fabric
```

This removes the isolated tool environment created by `uv tool install`.

## pipx

The `pipx` lane is release-installed and package-manager mediated:

```bash
pipx uninstall agent-workspace-fabric
```

This removes the isolated tool environment created by `pipx install`.

## Virtualenv / pip

Use this path only when you installed AWF into an active virtualenv with
`pip install agent-workspace-fabric`:

```bash
cd /path/to/project-or-env
. .venv/bin/activate
pip uninstall agent-workspace-fabric
deactivate
```

Remove the virtualenv directory only if it was dedicated to AWF and you no
longer need it.

## Source Checkout With Global Tool Install

This lane uses inspectable source plus a global tool installed from that source
checkout. If `~/.awf/config.yml` still points at this checkout, prepare to
refresh the persisted path while the global `awf` executable is still available.
Stop local Core before refreshing source-checkout metadata; `awf setup` checks
the API and Postgres host ports and blocks while the previous Core stack still
holds them. Then run:

```bash
cd /path/to/aira-agent-workspace-fabric
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
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
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
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
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

If there is no replacement checkout, edit `~/.awf/config.yml` and remove only
the top-level `source_checkout:` block. After the persisted metadata no longer
points at this checkout, remove the global tool:

```bash
uv tool uninstall agent-workspace-fabric
```

Then remove the checkout only if you no longer need the inspected source tree:

```bash
rm -rf /path/to/aira-agent-workspace-fabric
```

## Source Checkout With No Global Install

This lane uses inspectable source with no global install, so there is no global
`awf` executable to uninstall. After the persisted `source_checkout` metadata no
longer points at this checkout, remove the checkout only if you no longer need
the inspected source tree. To refresh the persisted path without a global `awf`
executable, use `uv run` from the current checkout before deleting it. Stop
local Core before refreshing source-checkout metadata; `awf setup` checks the
API and Postgres host ports and blocks while the previous Core stack still holds
them. Then run:

```bash
cd /path/to/aira-agent-workspace-fabric
AWF_PERSISTED_API_TOKEN=""
for env_file in .env docker/compose/.env; do
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
elif grep -q '^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=' .env docker/compose/.env 2>/dev/null; then
  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"
else
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_API_TOKEN
fi
AWF_PERSISTED_POSTGRES_PASSWORD=""
for env_file in .env docker/compose/.env; do
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
  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for the running local Core or persist it in .env or docker/compose/.env before refreshing source-checkout metadata}"
  export AWF_POSTGRES_PASSWORD
fi
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
```

Then remove the checkout:

```bash
rm -rf /path/to/aira-agent-workspace-fabric
```

## Local State

The commands above remove install artifacts only. They intentionally leave local
AWF service state, workspaces, logs, artifacts, Docker networks, and Docker
volumes alone. That separation prevents uninstall from deleting evidence or
workspace data by surprise.

Use [Troubleshooting](TROUBLESHOOTING.md) for targeted state cleanup when a
specific service, workspace, or Docker resource needs to be removed.
