# PRRT_kwDOSJAM6s6DDUfV Plan

## Problem Statement

The review thread reports that local service CLI commands resolve a missing
`docker/compose/.env` as the active env file even when a previously documented
repo-root `.env` exists. In that migration state, status, doctor, and bootstrap
readiness can ignore the values Docker Compose is actually using from the root
`.env`.

## Scope

- Fix only the local service env path selection in `src/awf/cli/main.py`.
- Preserve existing compose `.env` precedence when `docker/compose/.env` exists.
- Preserve first-run seeding behavior from root `.env` or examples when no
  readable env file exists yet.
- Add focused regression tests for root `.env` compatibility in service
  status, service doctor, and service bootstrap.

## Requirements Checklist

- [ ] Existing `docker/compose/.env` remains the active env file.
- [ ] Existing repo-root `.env` is returned as the active env file when compose
      `.env` is absent.
- [ ] Missing env files still seed `docker/compose/.env` from the best example.
- [ ] Validation covers the affected service CLI commands.

## Implementation Steps

1. Add failing service CLI regression tests for the root `.env` migration state.
2. Add a narrow helper that keeps the compose env as the seeding target but
   returns an existing root `.env` as the active read source when the compose
   env is absent.
3. Use that active env file for service status, service doctor, service
   bootstrap, and init preflight/bootstrap execution.
4. Run the affected CLI tests that cover command behavior and init seeding.

## Assumptions/Changes

The existing init regression requires root `.env` to keep seeding
`docker/compose/.env`; therefore the implementation separates the seeding target
from the active read source instead of changing `_resolve_service_compose_paths()`
to return root `.env` directly.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py
```

Pass criteria: the affected CLI test modules and lint pass without weakening
existing assertions.
