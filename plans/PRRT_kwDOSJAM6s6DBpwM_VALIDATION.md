# PRRT_kwDOSJAM6s6DBpwM Validation

Plan reference: `PRRT_kwDOSJAM6s6DBpwM_PLAN.md`

## Requirement Status

- Complete: Preserve the existing Docker preflight failure reason, message,
  action, and exit code.
  Evidence: the Docker preflight branch still emits `status`, `reason_code`,
  `message`, and `action`, and the regression asserts exit code 1 with
  `DOCKER_DAEMON_UNREACHABLE`.
- Complete: Include `env_error` in JSON Docker preflight failures when env
  seeding failed before preflight.
  Evidence:
  `test_init_without_path_json_includes_env_error_when_docker_preflight_fails`
  asserts the JSON payload includes the original write failure payload.
- Complete: Emit the pretty env seeding warning before Docker preflight can
  exit.
  Evidence:
  `test_init_without_path_warns_when_env_write_and_docker_preflight_fail`
  asserts the warning appears before the Docker failure notice.
- Complete: Avoid duplicate pretty warnings on successful bootstrap.
  Evidence: the early warning path records `env_warning_emitted`, and the full
  `tests/unit/cli/test_init.py` suite passes existing warning assertions.
- Complete: Add regression tests for JSON and pretty Docker preflight failures.
  Evidence: both new tests are present in `tests/unit/cli/test_init.py`.
- Complete: Run focused validation for the touched CLI tests.
  Evidence: commands below passed.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_json_includes_env_error_when_docker_preflight_fails tests/unit/cli/test_init.py::test_init_without_path_warns_when_env_write_and_docker_preflight_fail -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py
uv run --python 3.12 --extra dev ruff format --check src/awf/cli/main.py tests/unit/cli/test_init.py
uv run --python 3.12 --extra dev mypy src/awf
```

## Initial Regression Check

The focused test command failed before implementation because the JSON payload
had no `env_error` key and pretty output did not contain the env warning.

## Gaps

None.
