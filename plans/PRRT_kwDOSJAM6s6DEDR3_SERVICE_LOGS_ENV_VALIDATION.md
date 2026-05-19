# Validation: Pass Compose Env File To Service Logs

Plan reference: `plans/PRRT_kwDOSJAM6s6DEDR3_SERVICE_LOGS_ENV_PLAN.md`

## Requirement Status

- Complete: Added regressions proving `awf service logs` passes
  `docker/compose/.env` from a source checkout.
- Complete: Added a regression proving an existing root `.env` remains the
  fallback when the compose env file is missing.
- Complete: Kept follow, tail, and repeated service-filter command behavior
  unchanged while adding optional `--env-file` support.
- Complete: Updated the CLI reference to describe the env-file-backed wrapper
  command.
- Complete: Passed only env file paths to Docker Compose; no env contents are
  read or logged by the logs command.
- Complete: Ran targeted service logs unit tests and lint/type checks.

## Evidence

Changed files:

- `src/awf/service/logs.py`
- `src/awf/cli/main.py`
- `tests/unit/service/test_logs.py`
- `tests/unit/cli/test_service_cli.py`
- `docs/CLI_REFERENCE.md`

Verification commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_passes_source_checkout_compose_env_file tests/unit/cli/test_service_cli.py::test_service_logs_passes_existing_root_env_file_when_compose_env_is_missing -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/logs.py tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py
uv run --python 3.12 --extra dev mypy src/awf
```

Results:

- Focused regressions: 2 passed.
- Targeted logs suites: 82 passed.
- Ruff: passed.
- Mypy: passed.
