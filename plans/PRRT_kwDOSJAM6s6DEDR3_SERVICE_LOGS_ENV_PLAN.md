# Plan: Pass Compose Env File To Service Logs

## Problem Statement And Scope

The PR review thread `PRRT_kwDOSJAM6s6DEDR3` reports that `awf service logs`
does not pass the Compose environment file created by the quickstart bootstrap
path. In a source checkout, `awf init` writes `docker/compose/.env`, while the
logs command currently builds `docker compose -f docker/compose/local-service.yml
logs` without `--env-file`.

Scope is limited to the local service logs command path and its tests.

## Requirements Checklist

- Add a regression proving `awf service logs` passes the active
  `docker/compose/.env` file in source checkouts.
- Preserve fallback behavior for contexts that still use root `.env`.
- Keep follow and service-filter behavior unchanged.
- Avoid logging or exposing env file contents.
- Run targeted unit tests for the changed service logs surface.

## Implementation Steps

1. Extend `awf.service.logs.service_logs_command()` and `run_service_logs()` to
   accept an optional Compose env file and include `--env-file` before `-f`.
2. Update the CLI `service logs` command to resolve the same active env file as
   other local service commands and pass both compose file and env file to the
   logs helper.
3. Update existing logs tests and add a CLI regression for source-checkout
   `docker/compose/.env` resolution.
4. Update CLI reference text so the documented wrapper command reflects the
   env-file behavior.
5. Run targeted pytest coverage for service logs CLI and helper tests.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py -q
```

Pass criteria: the targeted tests pass, and command assertions show
`--env-file` is included with the resolved active env file.
