# Validation: Honor AWF_DOCKER_HOST for service logs

Plan reference:
`plans/PRRT_kwDOSJAM6s6DEUP5_SERVICE_LOGS_DOCKER_HOST_PLAN.md`

## Requirement Status

- Complete: Added a CLI regression proving `awf service logs` loads the active
  service env file and mirrors `AWF_DOCKER_HOST` into subprocess `DOCKER_HOST`.
- Complete: Added helper-level coverage proving explicit service environments
  override stale `DOCKER_HOST` values when running `docker compose logs`.
- Complete: Preserved existing log arguments, output handling, follow behavior,
  and structured failure behavior; the full service logs helper and CLI unit
  files pass.
- Complete: Kept the implementation scoped to `src/awf/service/logs.py` and the
  `service logs` CLI command in `src/awf/cli/main.py`.

## Evidence

Changed files:

- `src/awf/service/logs.py`
- `src/awf/cli/main.py`
- `tests/unit/service/test_logs.py`
- `tests/unit/cli/test_service_cli.py`

Failing regression evidence before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_mirrors_awf_docker_host_into_subprocess_env tests/unit/cli/test_service_cli.py::test_service_logs_mirrors_compose_awf_docker_host_into_subprocess_env -q`
- Result: failed because `run_service_logs()` did not accept
  `service_environ`, and the CLI subprocess call did not receive `env`.

Passing verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_mirrors_awf_docker_host_into_subprocess_env tests/unit/cli/test_service_cli.py::test_service_logs_mirrors_compose_awf_docker_host_into_subprocess_env -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

All planned requirements are complete; no follow-up iteration is needed.
