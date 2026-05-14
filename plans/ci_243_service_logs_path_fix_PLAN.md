# CI #243 Service Logs Path Fix Plan

## Problem Statement And Scope

PR #243 is failing CI because `awf service logs` now invokes `docker compose` with an absolute compose file path (`/workspace/docker/compose/local-service.yml`) while the command contract and existing CLI tests expect the relative path `docker/compose/local-service.yml`. The task scope is limited to fixing local-service compose path resolution behavior used by `awf service logs` and associated unit tests.

## Requirements Checklist

- [ ] Restore path behavior for default local service compose lookup so local-checkpoint commands use a relative path when `docker/compose/local-service.yml` is available from the current working directory.
- [ ] Preserve fallback resolution for local-service compose discovery when invoked from nested directories by continuing to return an absolute parent path.
- [ ] Keep error behavior and subprocess invocation semantics unchanged (`check`, `capture_output`, `text` flags, and CLI output handling).
- [ ] Ensure existing CLI and service log unit tests in the failing set pass.
- [ ] Add/update targeted regression coverage for relative-command behavior in CWD and keep existing parent-directory behavior covered.
- [ ] Run focused repro commands for the failing tests.

## Implementation Steps

1. Update `src/awf/service/logs.py` compose-file resolution helper to return the default relative path when present in the current working directory, while still resolving absolute paths when discovered from parent directories.
2. Run focused reproduction tests for:
   - `tests/unit/cli/test_openapi_artifact.py::test_spec_is_valid_openapi_3x`
   - `tests/unit/cli/test_service_cli.py::test_service_logs_defaults_to_tail_api_and_worker_logs`
   - `tests/unit/cli/test_service_cli.py::test_service_logs_accepts_repeated_service_filters`
   - `tests/unit/cli/test_service_cli.py::test_service_logs_follow_streams_without_capturing_subprocess_output`
3. Update/align service log tests if necessary to match intended behavior.
4. Produce validation file with results.

## Verification Commands And Pass Criteria

```bash
UV_PROJECT_ENVIRONMENT=.venv uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_openapi_artifact.py::test_spec_is_valid_openapi_3x \
  tests/unit/cli/test_service_cli.py::test_service_logs_defaults_to_tail_api_and_worker_logs \
  tests/unit/cli/test_service_cli.py::test_service_logs_accepts_repeated_service_filters \
  tests/unit/cli/test_service_cli.py::test_service_logs_follow_streams_without_capturing_subprocess_output \
  -q
```

Pass criteria: all four focused tests pass.
