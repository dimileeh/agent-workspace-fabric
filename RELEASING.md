# Releasing Agent Workspace Fabric

Use this checklist before tagging an AWF Core alpha release.

## Preconditions

- Work from a clean `development` branch.
- Confirm GitHub CI is green for the commit being tagged.
- Keep the public package name `agent-workspace-fabric` and the import package
  name `awf`.
- Confirm the supported install commands are documented and working:
  `uv tool install agent-workspace-fabric`, `pipx install
  agent-workspace-fabric`, virtualenv-scoped `pip install
  agent-workspace-fabric`, and contributor `uv tool install . --force`.
- The repository URL still points at
  `https://github.com/dimileeh/aira-agent-workspace-fabric` until the GitHub
  repository is renamed.

## Required Validation

```bash
git status --short --branch
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 \
  --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
npm --prefix apps/console ci
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
npm --prefix apps/console run test:browser
mkdir -p artifacts/release
uv run --python 3.12 --with build python -m build
sha256sum dist/* | tee artifacts/release/python-distribution-sha256.txt
docker build -t awf-control-plane:release-check -f docker/control-plane.Dockerfile .
docker build -t awf-agent-runtime:release-check -f docker/agent-runtime.Dockerfile .
```

Install the built wheel from outside the source checkout before tagging:

```bash
uv venv --python 3.12 /tmp/awf-release-install
cd /tmp
uv pip install --python /tmp/awf-release-install/bin/python \
  /path/to/aira-agent-workspace-fabric/dist/*.whl
/tmp/awf-release-install/bin/awf --help
/tmp/awf-release-install/bin/awf init --help
/tmp/awf-release-install/bin/awf service bootstrap --help
/tmp/awf-release-install/bin/python - <<'PY'
from pathlib import Path

from awf.service.bootstrap import get_bootstrap_asset_root

root = get_bootstrap_asset_root()
assert root is not None
for relative in (
    "docker/agent-runtime.Dockerfile",
    "docker/control-plane.Dockerfile",
    "docker/compose/local-service.yml",
    ".env.example",
    "openapi.json",
    "migrations/env.py",
):
    assert (Path(root) / relative).is_file(), relative
PY
```

## Dependency License Audit

Create release-local audit artifacts before tagging:

```bash
mkdir -p artifacts/release
uv run --python 3.12 --extra dev --with pip-licenses pip-licenses \
  --format=json \
  --output-file artifacts/release/python-licenses.json
npx --yes license-checker --production --json --start apps/console \
  > artifacts/release/node-licenses.json
```

Manually review any unknown, unlicensed, GPL, AGPL, LGPL, MPL, CDDL, EPL, or
custom licenses before tagging. Add a root `NOTICE` file only if the audit
finds a concrete attribution notice that must be preserved.

## Local Service Readiness

From a clean checkout with Docker running:

```bash
awf service bootstrap --timeout-seconds 300
awf service readiness --format json
awf service release-readiness --format pretty
```

Release readiness uses the lower-level service bootstrap command because it is
validating local service gates directly. Do not use no-path `awf init` for
service setup; project onboarding is the separate `awf init <path>` flow after
the local service is available.

If `awf service readiness` fails only because historical SLO evidence reflects
known dogfood failures, document the exception in the release notes and rerun
the gate with an explicit allowlist:

```bash
awf service readiness --allow-slo-breach --format json
```

Do not ignore doctor, provider, Docker, database, or cleanup failures.

## PyPI Trusted Publishing

AWF uses PyPI Trusted Publishing through GitHub OIDC; do not create or store a
long-lived PyPI API token for the release workflow. Before the first publish:

1. Create the `agent-workspace-fabric` project on TestPyPI/PyPI.
2. Configure Trusted Publishing for this repository and the `testpypi` and
   `pypi` GitHub environments.
3. Run the `Publish Python Package` workflow manually with
   `publish_target=testpypi`.
4. Install from TestPyPI in a disposable environment and run the local service
   bootstrap smoke above.
5. Only after TestPyPI is clean, run the workflow with `publish_target=pypi`
   for the tag being released.

The workflow also builds distributions on `v*` tags and uploads checksum
artifacts. Publishing remains manual until maintainers explicitly choose the
target environment.

## Homebrew Follow-Up

Homebrew is planned after one stable tagged PyPI/GitHub release. Before
advertising a Homebrew install path:

```bash
brew audit --strict --online agent-workspace-fabric
brew audit --new --formula agent-workspace-fabric
brew test agent-workspace-fabric
```

The formula should install from the tagged sdist or GitHub release tarball,
depend on Homebrew Python, and use `awf --help` as its smoke test.

## Tagging

```bash
git tag -a v0.1.0 -m "Agent Workspace Fabric v0.1.0"
git push origin v0.1.0
```
