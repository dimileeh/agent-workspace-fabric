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
gone. Before deleting a recorded checkout, either refresh the persisted path:

```bash
awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric
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
checkout. Remove the global tool when you no longer need a global `awf`
executable:

```bash
uv tool uninstall agent-workspace-fabric
```

After the persisted `source_checkout` metadata no longer points at this
checkout, remove the checkout only if you no longer need the inspected source
tree:

```bash
rm -rf /path/to/aira-agent-workspace-fabric
```

## Source Checkout With No Global Install

This lane uses inspectable source with no global install, so there is no global
`awf` executable to uninstall. After the persisted `source_checkout` metadata no
longer points at this checkout, remove the checkout only if you no longer need
the inspected source tree:

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
