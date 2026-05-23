# Distribution And Release Install Polish Plan

## Goal

Make released Python installs a first-class path for Agent Workspace Fabric:
`uv tool install agent-workspace-fabric`, `pipx install
agent-workspace-fabric`, and virtualenv-scoped `pip install
agent-workspace-fabric` should expose the `awf` command and carry enough
runtime assets for `awf init` and `awf service bootstrap` to work outside a
source checkout.

## Scope

- Keep the existing PyPA console-script entry point:
  `[project.scripts].awf = "awf.cli.main:app"`.
- Bundle the local service bootstrap build context into the wheel at a package
  data location, excluding local secret-bearing env files.
- Resolve bootstrap assets from either a verified source checkout or the
  bundled package assets.
- Keep package-install environment seeding local to the operator working
  directory (`.env`), not inside `site-packages`.
- Strengthen the CI release-artifact smoke so it installs the wheel from
  outside the source checkout and verifies CLI entry points plus packaged asset
  resolution.
- Add a manual/tag release workflow that builds artifacts on tags and can use
  PyPI Trusted Publishing via GitHub OIDC once package environments are
  configured.
- Update public docs and tests to prefer `uv tool`, document `pipx`, confine
  plain `pip install` to virtualenvs, and defer Homebrew until stable tagged
  artifacts exist.

## Out Of Scope

- Publishing to PyPI or TestPyPI from this task.
- Creating a Homebrew formula or tap in this task.
- Changing the local Docker service architecture.
- Reworking the control-plane Dockerfile beyond making its current context
  available from installed packages.

## Implementation Steps

1. Add bootstrap asset packaging metadata in `pyproject.toml` using
   wheel `force-include` mappings for Dockerfiles, Compose templates,
   `.env.example`, `pyproject.toml`, `uv.lock`, `README.md`, `alembic.ini`,
   migrations, source, docs, and `openapi.json`.
2. Add packaged asset resolution to `src/awf/service/bootstrap.py`, with a
   helper that identifies packaged asset roots.
3. Update service init path resolution in `src/awf/cli/main.py` so packaged
   installs seed/read local `.env` while still using the packaged compose file.
4. Add focused tests for packaged asset fallback, non-source `.env` seeding,
   wheel asset metadata, release workflow shape, and public docs.
5. Update README, Quickstart, Getting Started, Upgrade, Releasing, and MCP
   setup docs for the supported install paths and Homebrew deferral.
6. Add `.github/workflows/publish.yml` for build-on-tag plus manual
   TestPyPI/PyPI publishing through Trusted Publishing environments.
7. Validate with focused unit tests, lint, mypy, and a local wheel build/install
   smoke from outside the source checkout.

## Acceptance Criteria

- Built wheel contains the bootstrap build context assets required by local
  service bootstrap.
- `awf.service.bootstrap.get_bootstrap_asset_root()` resolves bundled assets
  when no source checkout is present.
- Package install mode never tries to write env files into packaged assets.
- CI verifies `awf --help`, `awf init --help`, `awf service bootstrap --help`,
  and packaged asset resolution from outside the checkout.
- Public docs mention `uv tool install`, `pipx install`, and virtualenv
  `pip install`, and do not advertise `brew install` as available.
- `RELEASING.md` documents Trusted Publishing, checksums, license artifacts,
  and Homebrew follow-up gates.
