# PRRT_kwDOSJAM6s6DSX5M Service Logs Docker Context Plan

## Problem Statement and Scope

The PR review thread reports that `awf service logs` mirrors an
`AWF_DOCKER_HOST` value into `DOCKER_HOST` for Docker CLI subprocesses but keeps
any inherited `DOCKER_CONTEXT`. Docker CLI context selection can override host
selection, so service-log daemon targeting remains nondeterministic when a
caller has a stale Docker context.

Scope is limited to the service log subprocess environment in
`src/awf/service/logs.py`.

## Requirements Checklist

- Add a regression test proving stale `DOCKER_CONTEXT` is removed when
  `AWF_DOCKER_HOST` is present.
- Preserve existing behavior that mirrors `AWF_DOCKER_HOST` into `DOCKER_HOST`.
- Preserve existing behavior that removes `AWF_DOCKER_HOST` from the Docker CLI
  subprocess environment.
- Keep unrelated Compose interpolation and Compose CLI environment behavior
  unchanged.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_logs.py` for
   `AWF_DOCKER_HOST` plus stale `DOCKER_CONTEXT`.
2. Update `_docker_cli_environ` in `src/awf/service/logs.py` to remove
   `DOCKER_CONTEXT` when an explicit Docker host is selected.
3. Run the focused regression test, then the service logs unit test file.
4. Run focused lint for the touched Python files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_clears_docker_context_when_awf_docker_host_is_forced -q`
  fails before the implementation change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`
  passes.
