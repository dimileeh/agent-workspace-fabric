# Contributing To AWF Core

AWF Core is the local open-source control plane for isolated agent workspaces.
Contributions should preserve the product contract in `docs/awf_prd_v2.2.md`
and the local trust model in `docs/AWF_CORE_TRUST_MODEL.md`.

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
- Update `TODO/pre-gke-industrial-readiness.md` when a P0/P1 readiness item
  changes state.
- Redact secrets from logs, screenshots, and fixtures.

## Local Versus Future Hosted Support

This repository currently targets local AWF Core. GKE, hosted control planes,
cloud secret brokers, and multi-tenant hardening are future/cloud layers unless
explicitly marked as Core work.
