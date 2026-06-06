# Uninstall Guide

Use the uninstall path that matches the lane you used for first run.

Uninstalling the CLI does not delete local AWF service state, workspace state,
Docker volumes, logs, or artifacts. Preserve or clean those separately according
to your local data-retention needs.

The public curl installer lane is release-gated until the hosted installer URL,
manifest, checksums, and release artifacts are published and verified.

## Hosted uninstaller (`uninstall.sh`)

AWF ships `packaging/uninstall.sh`, a standalone, inspected uninstaller that is
the symmetric counterpart to the hosted installer. Like the installer lane it is
release-gated until the hosted URL and release artifacts are published and
verified; until then, run the checked-in script directly. It is conservative by
design:

- **Credentials are preserved by default.** It never removes provider secrets,
  keyring entries, env refs, or `gh`/agent auth, and there is no flag that does.
- **Default action** removes only an AWF-managed `uv`/`pipx` package — exactly
  like `install.sh --uninstall`. An `awf` not installed by `uv`/`pipx` is refused
  (`UNINSTALL_REFUSED_UNMANAGED`); nothing installed is a clean no-op.
- **Opt-in state cleanup** removes only an explicit allowlist under `~/.awf`:
  `--purge-config` removes `~/.awf/config.yml` and `--purge-state` removes
  `~/.awf/service` (`--all` does both). It never removes `~/.awf` itself or any
  other entry, so a secrets store beside them survives.
- **`--remove-uv`** removes `uv` only when an AWF ownership marker proves AWF
  bootstrapped it (`~/.awf/uv-bootstrap.marker`); otherwise it refuses
  (`UV_REMOVAL_REFUSED_UNOWNED`) so a `uv` you installed yourself is never
  removed.
- **`--dry-run`** plans every action and mutates nothing.
- **Destructive filesystem cleanup** (`--purge-config`/`--purge-state`/
  `--remove-uv`) requires `--yes` or an interactive confirmation; non-interactive
  without `--yes` fails closed with `CONFIRMATION_REQUIRED`.

```bash
# Preview what a full local cleanup would remove (mutates nothing):
bash packaging/uninstall.sh --all --remove-uv --dry-run

# Remove the managed package plus AWF config and state, non-interactively:
bash packaging/uninstall.sh --all --yes
```

Docker volumes and Compose stacks are left intact; stop them with
`awf service gc` or `docker compose down` before uninstalling if desired.

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
