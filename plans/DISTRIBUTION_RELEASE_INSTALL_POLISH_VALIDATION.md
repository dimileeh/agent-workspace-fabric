# Distribution And Release Install Polish Validation

## Plan Match

Implemented against `plans/DISTRIBUTION_RELEASE_INSTALL_POLISH_PLAN.md`.

## Completed

- Added wheel package-data mappings for the AWF local bootstrap build context:
  Dockerfiles, Compose files, `.env.example`, `pyproject.toml`, `uv.lock`,
  `README.md`, `alembic.ini`, migrations, source, docs, and `openapi.json`.
- Added packaged bootstrap asset resolution in `awf.service.bootstrap`, with
  source-checkout precedence and packaged-asset fallback.
- Kept package-install env seeding local: package assets provide the Compose
  file and seed example, while `.env` is read/written in the operator working
  directory rather than under `site-packages`.
- Expanded CI release-artifact smoke to install the wheel from `/tmp`, verify
  `awf`, `awf init`, and `awf service bootstrap` help, and assert packaged
  bootstrap assets are present while `docker/compose/.env` is absent.
- Added `.github/workflows/publish.yml` for tag builds and manual TestPyPI/PyPI
  Trusted Publishing via GitHub OIDC environments.
- Updated public docs and release checklist for `uv tool`, `pipx`, virtualenv
  `pip install`, contributor `uv tool install . --force`, Trusted Publishing,
  checksums, and Homebrew deferral.

## Validation

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/docs \
  tests/unit/cli/test_packaging.py \
  tests/unit/service/test_bootstrap.py \
  tests/unit/cli/test_init.py \
  tests/unit/test_ci_workflow_full_coverage.py \
  -q
# 238 passed

uv run --python 3.12 --extra dev ruff check src/awf tests
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 164 source files

uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
# OK: openapi.json matches the current app spec
```

Installed-wheel smoke from outside the checkout:

```bash
tmpdir=$(mktemp -d)
uv run --python 3.12 --with build python -m build --wheel --outdir "$tmpdir/dist"
uv venv --python 3.12 "$tmpdir/venv"
cd /tmp
uv pip install --python "$tmpdir/venv/bin/python" "$tmpdir"/dist/*.whl
"$tmpdir/venv/bin/awf" --help
"$tmpdir/venv/bin/awf" init --help
"$tmpdir/venv/bin/awf" service bootstrap --help
"$tmpdir/venv/bin/python" - <<'PY'
from pathlib import Path
from awf.service.bootstrap import get_bootstrap_asset_root

root = get_bootstrap_asset_root()
assert root is not None
required = [
    "docker/agent-runtime.Dockerfile",
    "docker/control-plane.Dockerfile",
    "docker/compose/local-service.yml",
    "docker/compose/workspace.base.yml.j2",
    ".env.example",
    "openapi.json",
    "pyproject.toml",
    "src/awf/__init__.py",
    "migrations/env.py",
]
missing = [relative for relative in required if not (Path(root) / relative).is_file()]
assert not missing, missing
assert not (Path(root) / "docker/compose/.env").exists()
PY
# passed
```

Built both sdist and wheel from sdist, then inspected both archives for the
required runtime assets and confirmed `docker/compose/.env` was absent:

```bash
tmpdir=$(mktemp -d)
uv run --python 3.12 --with build python -m build --outdir "$tmpdir/dist"
# Successfully built agent_workspace_fabric-0.1.0.tar.gz and
# agent_workspace_fabric-0.1.0-py3-none-any.whl
```

Note: bare `python scripts/generate_openapi.py --check` failed in this local
shell because the bare interpreter did not have project dependencies installed.
The project-standard `uv run --python 3.12 --extra dev ...` invocation passed.

Review follow-up:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/docs/test_public_docs_status.py \
  tests/unit/cli/test_packaging.py \
  tests/unit/service/test_bootstrap.py \
  tests/unit/cli/test_init.py \
  tests/unit/test_ci_workflow_full_coverage.py \
  -q
# 219 passed

uv run --python 3.12 --extra dev pytest \
  tests/unit/docs/test_public_docs_status.py \
  tests/unit/cli/test_packaging.py \
  tests/unit/test_ci_workflow_full_coverage.py \
  -q
# 34 passed
```

The review follow-up switched public OpenAPI regeneration docs to the
project-standard `uv run --python 3.12 --extra dev python ...` invocation and
added Homebrew's new-formula audit command to the release checklist.

## Gaps

- No PyPI/TestPyPI publish was performed. The workflow is present, but
  maintainers still need to configure Trusted Publishing environments before
  using it.
- Homebrew remains intentionally deferred until stable tagged Python artifacts
  exist and a formula can pass Homebrew audit/test gates.
