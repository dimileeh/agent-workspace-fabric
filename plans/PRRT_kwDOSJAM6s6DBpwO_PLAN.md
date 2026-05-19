# PRRT_kwDOSJAM6s6DBpwO Plan

## Problem Statement

The getting-started workflow writes local service configuration to
`docker/compose/.env` and then runs `awf service bootstrap`. The direct
`service bootstrap` CLI currently resolves `ServiceSettings` from the default
settings source, so readiness polling can use different values than Compose
when important settings exist only in the Compose env file.

## Requirements

- Resolve `awf service bootstrap` settings from the same Compose env file and
  merged environment used by the bootstrap helper.
- Preserve host environment override behavior.
- Add a regression test that fails when `AWF_DATABASE_URL`,
  `AWF_DOCKER_HOST`, or `AWF_API_BASE_URL` in `docker/compose/.env` are ignored.
- Keep the change scoped to the review thread.

## Implementation Steps

1. Add a focused CLI regression test for direct `awf service bootstrap`.
2. Update the CLI bootstrap command to build `Settings` from the local service
   env file and pass the merged local-service environment into
   `resolve_service_settings`.
3. Run the targeted unit tests for the touched CLI behavior.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
- Pass criteria: the new regression and existing service CLI tests pass.
