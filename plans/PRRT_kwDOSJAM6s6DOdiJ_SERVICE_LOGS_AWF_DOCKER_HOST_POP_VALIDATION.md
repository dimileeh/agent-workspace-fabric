# Validation: Keep AWF_DOCKER_HOST out of service logs subprocess env

Plan reference:
`plans/PRRT_kwDOSJAM6s6DOdiJ_SERVICE_LOGS_AWF_DOCKER_HOST_POP_PLAN.md`

## Requirement Status

- Complete: Added a regression proving Compose interpolation cannot reintroduce
  `AWF_DOCKER_HOST` into the subprocess environment.
- Complete: Preserved mirroring of the resolved Docker host into `DOCKER_HOST`.
- Complete: Preserved existing Compose interpolation and Compose CLI environment
  behavior by changing only the final removal order for `AWF_DOCKER_HOST`.
- Complete: Kept implementation scope to `src/awf/service/logs.py`,
  `tests/unit/service/test_logs.py`, and required plan/validation artifacts.

## Evidence

Changed files:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs.py`
- `plans/PRRT_kwDOSJAM6s6DOdiJ_SERVICE_LOGS_AWF_DOCKER_HOST_POP_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DOdiJ_SERVICE_LOGS_AWF_DOCKER_HOST_POP_VALIDATION.md`

Failing regression evidence before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_awf_docker_host_after_compose_interpolation -q`
- Result: failed because `AWF_DOCKER_HOST` was present in the subprocess
  environment after Compose interpolation values were merged.

Passing verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_awf_docker_host_after_compose_interpolation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`

All planned requirements are complete; no follow-up iteration is needed.
