# Plan: Honor AWF_DOCKER_HOST for service logs

## Problem Statement and Scope

The unresolved PR review thread `PRRT_kwDOSJAM6s6DEUP5` reports that `awf service logs`
passes the resolved Compose env file to `docker compose` for interpolation, but
still lets the Docker client inherit the process environment. When
`AWF_DOCKER_HOST` is defined only in the resolved service env file, logs can be
read from the wrong Docker daemon.

Scope is limited to the local service logs command path and focused tests.

## Requirements Checklist

- Add a regression proving `awf service logs` loads the active service env file
  and mirrors `AWF_DOCKER_HOST` into subprocess `DOCKER_HOST`.
- Add helper-level coverage proving explicit service environments override stale
  `DOCKER_HOST` values when running `docker compose logs`.
- Preserve existing `docker compose logs` arguments, output handling, follow
  behavior, and structured failure behavior.
- Keep the fix scoped to service logs; do not change bootstrap, status, doctor,
  or unrelated service commands.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_logs.py` and
   `tests/unit/cli/test_service_cli.py` for the missing Docker client env.
2. Extend `awf.service.logs.run_service_logs()` to accept an optional resolved
   service environment and pass Docker-client environment only when a Docker host
   is configured.
3. Update the CLI `service logs` command to load `local_service_environ()` from
   the active env file and pass it to `run_service_logs()`.
4. Run the targeted service log tests, then run the narrow lint/type checks for
   touched Python surfaces.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: targeted tests pass, lint passes, mypy passes, and validation
documents all checklist items as complete or explicitly explains any gap.
