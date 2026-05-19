# Plan: Keep AWF_DOCKER_HOST out of service logs subprocess env

## Problem Statement and Scope

The unresolved PR review thread `PRRT_kwDOSJAM6s6DOdiJ` reports that
`_docker_cli_environ()` removes `AWF_DOCKER_HOST` before merging Compose
interpolation values. If the Compose file references `${AWF_DOCKER_HOST}`,
that merge can reintroduce the key into the `docker compose logs` subprocess
environment.

Scope is limited to the service logs environment construction and a focused
unit regression.

## Requirements Checklist

- Add a regression proving Compose interpolation cannot reintroduce
  `AWF_DOCKER_HOST` into the subprocess environment.
- Preserve mirroring of the resolved Docker host into `DOCKER_HOST`.
- Preserve Compose interpolation and Compose CLI environment behavior for other
  keys.
- Keep the implementation scoped to `src/awf/service/logs.py` and its service
  log tests.

## Implementation Steps

1. Add a failing regression in `tests/unit/service/test_logs.py` where the
   Compose file references `${AWF_DOCKER_HOST}`.
2. Move the `AWF_DOCKER_HOST` removal after all environment merges in
   `_docker_cli_environ()`.
3. Run the targeted regression and the service log unit tests.
4. Run focused lint for the touched Python files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_awf_docker_host_after_compose_interpolation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`

Pass criteria: the new regression fails before implementation, then all listed
commands pass after implementation.
