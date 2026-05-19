# Review 4482045018 Docker Host Order Plan

## Problem Statement and Scope

The PR review reports that `src/awf/service/logs.py` derives `DOCKER_HOST` from
`AWF_DOCKER_HOST`, but then merges Compose interpolation values afterward. If the
compose file interpolates `DOCKER_HOST`, the later merge can replace the
AWF-managed Docker host with a service env or compose env-file value.

Scope is limited to Docker Compose logs subprocess environment construction and a
regression test covering the precedence rule.

## Requirements Checklist

- Add a regression test that fails when Compose interpolation can clobber an
  `AWF_DOCKER_HOST`-derived `DOCKER_HOST`.
- Preserve existing behavior that Compose interpolation values and Compose CLI
  variables override stale caller environment values.
- Ensure explicit `AWF_DOCKER_HOST` remains the final source for `DOCKER_HOST`.
- Keep `AWF_DOCKER_HOST` itself out of the subprocess environment.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_logs.py` that creates a
   compose file referencing `${DOCKER_HOST}`, provides distinct
   `AWF_DOCKER_HOST` and `DOCKER_HOST` values in `service_environ`, and asserts
   the subprocess receives the `AWF_DOCKER_HOST` value as `DOCKER_HOST`.
2. Run the focused test and confirm it fails before the implementation change.
3. Update `_docker_cli_environ` so `resolved["DOCKER_HOST"]` is assigned after
   `compose_env` and `compose_cli_env` are merged.
4. Run the focused logs tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`

Pass criteria: the new regression and existing local service log tests pass, and
the subprocess env no longer exposes `AWF_DOCKER_HOST`.
