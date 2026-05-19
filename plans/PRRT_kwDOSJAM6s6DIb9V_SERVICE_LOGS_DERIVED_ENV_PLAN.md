# Plan: Preserve Derived Compose Env For Service Logs

## Problem Statement And Scope

The unresolved PR review thread `PRRT_kwDOSJAM6s6DIb9V` reports that
`awf service logs` can drop `AWF_POSTGRES_PASSWORD` after
`local_service_environ()` derives it from `AWF_DATABASE_URL`. Docker Compose
still interpolates `${AWF_POSTGRES_PASSWORD:?set AWF_POSTGRES_PASSWORD}` when
running `docker compose logs`, so the logs command can fail even though other
service commands used the resolved environment successfully.

Scope is limited to the local service logs subprocess environment and focused
tests.

## Requirements Checklist

- Add a regression proving service logs pass derived `AWF_POSTGRES_PASSWORD`
  into the Docker Compose subprocess even when no Docker host override is set.
- Add coverage proving a resolved service environment overrides stale caller
  `AWF_POSTGRES_PASSWORD` values for Compose interpolation.
- Preserve the existing safety contract that service logs do not copy unrelated
  service secrets such as API tokens or database URLs into the subprocess
  environment.
- Preserve existing service logs command arguments, follow behavior, output
  handling, and structured failure behavior.
- Run the targeted service logs tests and narrow lint for touched files.
- Commit the scoped fix locally without switching branches or pushing.

## Implementation Steps

1. Add failing helper-level tests in `tests/unit/service/test_logs.py` for the
   derived Compose password and stale caller override behavior.
2. Update `awf.service.logs._docker_cli_environ()` to build a subprocess
   environment when resolved Compose interpolation values need to override the
   caller environment, while continuing to mirror Docker host selection.
3. Keep unrelated service secrets out of the subprocess environment.
4. Run targeted pytest and ruff checks.
5. Create the validation document with requirement-by-requirement evidence.
6. Stage only changed files and commit with the thread id in the message.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py
```

Pass criteria: targeted tests pass, lint passes, and validation documents all
requirements as complete or explains any remaining gap.
