# PRRT_kwDOSJAM6s6DSX5M Service Logs Docker Context Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DSX5M_SERVICE_LOGS_DOCKER_CONTEXT_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving stale `DOCKER_CONTEXT` is removed
  when `AWF_DOCKER_HOST` is present.
- Complete: Preserved existing behavior that mirrors `AWF_DOCKER_HOST` into
  subprocess `DOCKER_HOST`.
- Complete: Preserved existing behavior that removes `AWF_DOCKER_HOST` from the
  Docker CLI subprocess environment.
- Complete: Kept unrelated Compose interpolation and Compose CLI environment
  behavior unchanged.

## Evidence

Files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs.py`
- `plans/PRRT_kwDOSJAM6s6DSX5M_SERVICE_LOGS_DOCKER_CONTEXT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DSX5M_SERVICE_LOGS_DOCKER_CONTEXT_VALIDATION.md`

Failing regression evidence before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_clears_docker_context_when_awf_docker_host_is_forced -q`
  failed because `DOCKER_CONTEXT` remained in the subprocess environment.

Passing verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_clears_docker_context_when_awf_docker_host_is_forced -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
  passed with 30 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
