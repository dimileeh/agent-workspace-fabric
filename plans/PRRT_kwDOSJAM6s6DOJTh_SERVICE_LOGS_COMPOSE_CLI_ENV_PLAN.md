# Plan: Preserve Compose CLI Env For Service Logs

## Problem Statement And Scope

The unresolved PR review thread `PRRT_kwDOSJAM6s6DOJTh` reports that
`awf service logs` receives the resolved local service environment but rebuilds
the Docker subprocess environment from the caller process plus only Docker host
selection and Compose interpolation values. That can drop Compose CLI controls
such as `COMPOSE_PROJECT_NAME` and `COMPOSE_PROFILES` that were loaded from the
active Compose env file, causing logs to target the wrong project or omit
profile-scoped services.

Scope is limited to the local service logs subprocess environment and focused
unit tests.

## Requirements Checklist

- Add a regression proving service logs preserve resolved Compose CLI variables
  needed to select the same Compose project/profile as bootstrap.
- Preserve the existing safety contract that service logs do not copy ordinary
  service secrets such as API tokens or database URLs into the subprocess
  environment.
- Preserve existing Docker host override and Compose interpolation behavior.
- Preserve existing service logs command arguments, follow behavior, output
  handling, and structured failure behavior.
- Run targeted service logs tests and narrow lint for touched source/test files.
- Commit the scoped fix locally without switching branches or pushing.

## Implementation Steps

1. Add a failing helper-level regression in `tests/unit/service/test_logs.py`
   for `COMPOSE_PROJECT_NAME` and `COMPOSE_PROFILES` coming from
   `service_environ` when the caller environment has stale or missing values.
2. Update `awf.service.logs._docker_cli_environ()` to merge a narrowly scoped
   set of resolved Compose CLI variables into the subprocess environment.
3. Keep AWF service secrets and unrelated service values out of the subprocess
   environment.
4. Run the focused failing regression, then the full service logs unit module
   and ruff for the touched files.
5. Create the validation document with requirement-by-requirement evidence.
6. Stage only changed files and commit with the thread id in the message.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_preserves_compose_cli_vars_from_resolved_env -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py
```

Pass criteria: the regression fails before implementation, then all targeted
tests and lint pass after the fix.
