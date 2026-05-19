# PRRT_kwDOSJAM6s6DC_rM Plan

## Problem Statement And Scope

PR #264 review thread `PRRT_kwDOSJAM6s6DC_rM` reports that
`run_service_bootstrap()` passes the merged local service environment to Docker
bootstrap stages, but does not mirror `AWF_DOCKER_HOST` into the Docker CLI's
`DOCKER_HOST` variable. When `AWF_DOCKER_HOST` comes only from the resolved
`docker/compose/.env`, preflight and status can use the configured Docker host
while `docker build` and `docker compose` stages use Docker's default host.

Scope is limited to local service bootstrap stage environment normalization and
focused unit coverage.

## Requirements Checklist

- [ ] Add a regression test proving bootstrap Docker stages receive
  `DOCKER_HOST` when the merged service environment contains only
  `AWF_DOCKER_HOST`.
- [ ] Preserve existing behavior when no `AWF_DOCKER_HOST` is present.
- [ ] Keep the readiness/status poll environment aligned with the stage
  environment.
- [ ] Avoid broad refactors or changes to CLI branch/push behavior.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_bootstrap.py`.
2. Run the new test and confirm it fails against the current implementation.
3. Update `src/awf/service/bootstrap.py` to mirror `AWF_DOCKER_HOST` into
   `DOCKER_HOST` before running bootstrap stages and polling status.
4. Re-run the focused test, then the bootstrap unit test file and focused lint.
5. Record validation in `plans/PRRT_kwDOSJAM6s6DC_rM_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_mirrors_awf_docker_host_to_docker_cli_environment -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py tests/unit/service/test_bootstrap.py
```

Pass criteria: the focused regression fails before implementation, then all
listed commands pass after implementation.
