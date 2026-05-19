# PRRT_kwDOSJAM6s6DSEeO Docker Context Plan

## Problem Statement and Scope

The PR review thread reports that `src/awf/service/bootstrap.py` mirrors
`AWF_DOCKER_HOST` into `DOCKER_HOST` for Docker CLI subprocesses but leaves an
existing `DOCKER_CONTEXT` in place. Because Docker CLI context selection can
override host selection, bootstrap should make daemon targeting deterministic
when AWF has resolved an explicit Docker host.

Scope is limited to the local service bootstrap subprocess environment used by
`run_service_bootstrap`.

## Requirements Checklist

- Add a regression test proving stale `DOCKER_CONTEXT` is removed when
  `AWF_DOCKER_HOST` is present.
- Preserve existing behavior that removes `AWF_DOCKER_HOST` before invoking
  Docker subprocesses.
- Preserve existing behavior for stale `DOCKER_HOST` replacement.
- Keep changes scoped to bootstrap review feedback.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_bootstrap.py` covering
   `AWF_DOCKER_HOST` plus stale `DOCKER_CONTEXT`.
2. Update `_docker_cli_environ` in `src/awf/service/bootstrap.py` to remove
   `DOCKER_CONTEXT` when `AWF_DOCKER_HOST` is used.
3. Run the narrow bootstrap unit test surface.
4. Run formatting/lint/type checks that are relevant to the touched Python
   files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py tests/unit/service/test_bootstrap.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any unrelated pre-existing failure is documented.
