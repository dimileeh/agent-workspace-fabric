# Contributing To Agent Workspace Fabric

Agent Workspace Fabric (AWF) Core is the local open-source control plane for
isolated agent workspaces. Contributions should preserve the product contract
in `docs/awf_prd_v2.2.md` and the local trust model in
`docs/AWF_CORE_TRUST_MODEL.md`.

## Development Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
npm --prefix apps/console ci
```

## Validation

Use the narrowest test that proves your change, then expand when the touched
surface is shared:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
```

## Required GitHub Status Check

Branch protection for `main` and `development` must require the CI
`ci-required` job. That rollup job depends on `lint-and-type`,
`python-full-coverage`, `console`, and `release-artifacts`, so the 99%
coverage gate cannot be bypassed by other green jobs in the workflow.
The long Python coverage run is fanned out by `python-coverage-shards`;
`python-full-coverage` downloads those shard artifacts, combines them, and
enforces the exact 99% threshold.

Before a local Core release, run:

```bash
uv run --python 3.12 --extra dev awf service readiness --format json
```

## Pull Request Expectations

- Keep changes scoped to one behavior or release-readiness slice.
- Add regression tests for bugs and contract tests for public API/CLI/MCP
  surfaces.
- Preserve failure reason codes and logs. Do not hide failures behind generic
  retries.
- Redact secrets from logs, screenshots, and fixtures.

## Local Versus Future Hosted Support

This repository currently targets local AWF Core. GKE, hosted control planes,
cloud secret brokers, and multi-tenant hardening are future/cloud layers unless
explicitly marked as Core work.

### Contributor Setup

```bash
git clone git@github.com:dimileeh/agent-workspace-fabric.git
cd agent-workspace-fabric

uv sync --extra dev
```

Run tests:

```bash
uv run --python 3.12 --extra dev pytest -q
```

Run lint and types:

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev mypy
```

The local pre-commit hooks `awf-ruff-check`, `awf-ruff-format-check`, and
`awf-mypy` run these checks without auto-fixing. Fix reported issues manually,
then re-run the same command or `pre-commit run --all-files` before committing.

### Build the Agent Runtime Image

AWF workspaces use `awf-agent-runtime:latest` unless configured otherwise.
The image includes the Docker CLI, Docker Compose plugin, and the Docker
Buildx plugin so DinD profiles can run project Compose diagnostics (with
BuildKit, not the legacy builder) inside the workspace sidecar. Rebuild this
image whenever the runtime Dockerfile or those Docker tooling packages change.

```bash
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
```

Verify:

```bash
docker image inspect awf-agent-runtime:latest
```

### Database Migrations

The control-plane database is PostgreSQL. The preferred bootstrap command runs
Alembic migrations through the Compose `migrate` service before starting the API
and worker:

```bash
uv run --python 3.12 --extra dev awf service bootstrap
```

Manual Compose migration:

```bash
docker compose -f docker/compose/local-service.yml up --build --force-recreate migrate
```

Manual Postgres migration:

```bash
AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf \
  uv run --python 3.12 --extra dev alembic upgrade head
```

## Development Workflow

Before pushing changes:

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev mypy
uv run --python 3.12 --extra dev pytest -q
```

Useful focused commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```
