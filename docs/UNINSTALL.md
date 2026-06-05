# Uninstall Guide

Use the uninstall path that matches the lane you used for first run.

Uninstalling the CLI does not delete local AWF service state, workspace state,
Docker volumes, logs, or artifacts. Preserve or clean those separately according
to your local data-retention needs.

The public curl installer lane is release-gated until the hosted installer URL,
manifest, checksums, and release artifacts are published and verified.

## uv tool

```bash
uv tool uninstall agent-workspace-fabric
```

## pipx

```bash
pipx uninstall agent-workspace-fabric
```

## Virtualenv / pip

Activate the virtualenv that owns `awf`, then uninstall the package:

```bash
. .venv/bin/activate
pip uninstall agent-workspace-fabric
```

## Source Checkout With Global Tool Install

If you installed a global executable from a source checkout, remove that tool
environment:

```bash
uv tool uninstall agent-workspace-fabric
```

Keep or delete the source checkout according to whether you still need to inspect
or develop AWF.

## Source Checkout With No Global Install

There is no global executable to uninstall in this lane. Keep or delete the
source checkout according to whether you still need to inspect or develop AWF.

## Local Core State

The commands above remove the CLI entrypoint for the selected lane. They do not
remove the local Core project, workspace directories, or Docker volumes. From a
source checkout, `docker compose down` stops the local Compose project without
removing volumes.
