# Releasing Agent Workspace Fabric

Use this checklist before tagging an AWF Core alpha release.

## Preconditions

- Work from a clean `development` branch.
- Confirm GitHub CI is green for the commit being tagged.
- Keep the public package name `agent-workspace-fabric` and the import package
  name `awf`.
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
python scripts/generate_openapi.py --check
npm --prefix apps/console ci
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
npm --prefix apps/console run test:browser
uv run --python 3.12 --with build python -m build
docker build -t awf-control-plane:release-check -f docker/control-plane.Dockerfile .
docker build -t awf-agent-runtime:release-check -f docker/agent-runtime.Dockerfile .
```

## Dependency License Audit

Create release-local audit artifacts before tagging:

```bash
mkdir -p artifacts/release
uv run --python 3.12 --extra dev --with pip-licenses pip-licenses \
  --format=json \
  --output-file artifacts/release/python-licenses.json
npm --prefix apps/console ci
npx --yes license-checker --production --json --start apps/console \
  > artifacts/release/node-licenses.json
```

Manually review any unknown, unlicensed, GPL, AGPL, LGPL, MPL, CDDL, EPL, or
custom licenses before tagging. Add a root `NOTICE` file only if the audit
finds a concrete attribution notice that must be preserved.

## Local Service Readiness

From a clean checkout with Docker running:

```bash
awf init
awf service bootstrap --timeout-seconds 300
awf service readiness --format json
awf service release-readiness --format pretty
```

If `awf service readiness` fails only because historical SLO evidence reflects
known dogfood failures, document the exception in the release notes and rerun
the gate with an explicit allowlist:

```bash
awf service readiness --allow-slo-breach --format json
```

Do not ignore doctor, provider, Docker, database, or cleanup failures.

## Tagging

```bash
git tag -a v0.1.0 -m "Agent Workspace Fabric v0.1.0"
git push origin v0.1.0
```
