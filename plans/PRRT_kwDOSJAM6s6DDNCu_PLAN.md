# PRRT_kwDOSJAM6s6DDNCu Plan

## Problem Statement

The unresolved PR review thread reports that `awf init` seeds `docker/compose/.env`
from an example template when a source checkout already has a working root
`.env`. Because bootstrap then passes `docker/compose/.env` explicitly to Docker
Compose, values from the root `.env` stop being used and required Compose
variables can fail interpolation.

## Scope

- Keep the fix limited to local-service init env seeding.
- Preserve existing behavior when `docker/compose/.env` already exists.
- Preserve existing source-checkout targeting of `docker/compose/.env`.
- Prefer an existing source-root `.env` before example templates when the
  compose env target is missing.

## Requirements Checklist

- Add a regression test proving root `.env` is copied to `docker/compose/.env`
  for a verified AWF source checkout when the compose env target is absent.
- Ensure example-template seeding remains the fallback when no root `.env`
  exists.
- Do not print secret values from the migrated `.env`.
- Keep changes scoped to the CLI env-seeding path and related tests/docs.

## Implementation Steps

1. Add a failing unit test in `tests/unit/cli/test_init.py` for root `.env`
   migration into `docker/compose/.env`.
2. Update `_resolve_service_compose_paths()` so the seed source priority for a
   verified source checkout is:
   `root .env`, `docker/compose/.env.example`, then root `.env.example`.
3. Update CLI help text if needed so it does not claim examples are the only
   seed source.
4. Run the focused test first, then the relevant CLI test file and ruff on the
   touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::<new-test> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
