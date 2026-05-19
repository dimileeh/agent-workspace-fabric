# Release Readiness Compose Env Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DGh-i` reports that the quickstart now makes
`docker/compose/.env` the persisted local-service env for source-checkout users,
but `awf service readiness` still resolves settings and provider env from the
default environment. That can make the release-readiness gate ignore values such
as `AWF_DATABASE_URL`, `AWF_API_BASE_URL`, and provider tokens that only exist in
the Compose env file.

Scope is limited to the CLI release-readiness/readiness command and focused
regression coverage. No GitHub write actions or branch changes are in scope.

## Requirements Checklist

- `awf service readiness` and its `release-readiness` alias must resolve
  `ServiceSettings` from the same existing local service env file used by
  `service status` and `service doctor`.
- The readiness collector must receive the merged service env for both
  `provider_environ` and `environ`.
- Existing strict-provider validation, release-gate output, and nonzero failure
  behavior must remain unchanged.
- Add a regression test proving Compose-only readiness settings are honored.

## Implementation Steps

1. Add a failing CLI regression test with `docker/compose/.env` values and no
   matching host env, capturing settings and env passed to the readiness
   collector.
2. Update `service_readiness` to resolve the active service env path, merge it
   with `local_service_environ(env_file=...)`, and call
   `resolve_service_settings(Settings(_env_file=...), environ=service_env)`.
3. Update existing readiness tests whose monkeypatches assume no env-file
   parameters.
4. Run the focused service CLI readiness tests, then run the broader service CLI
   unit test file if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  must pass, or any remaining failure must be documented as unrelated.
