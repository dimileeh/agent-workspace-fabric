# Uninstall Guide

Use the uninstall path that matches the first-run lane you chose. Uninstalling
the CLI or source checkout does not delete local AWF service state, workspace
state, Docker volumes, logs, or artifacts. The hosted curl installer lane is
intentionally omitted until its public installer, manifest, checksums, and
distribution artifacts are published and verified.

For destructive cleanup, use the targeted steps in
[Troubleshooting](TROUBLESHOOTING.md) only when you intentionally want to remove
local state.

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

## Source Checkout With Global Tool Install

This lane uses inspectable source plus a global tool installed from that source
checkout. Remove the global tool first:

```bash
uv tool uninstall agent-workspace-fabric
```

Then remove the checkout only if you no longer need the inspected source tree:

```bash
rm -rf /path/to/aira-agent-workspace-fabric
```

## Source Checkout With No Global Install

This lane uses inspectable source with no global install, so there is no global
`awf` executable to uninstall. Remove the checkout only if you no longer need
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
