# Validation: Preserve Compose CLI Env For Service Logs

Plan reference:
`plans/PRRT_kwDOSJAM6s6DOJTh_SERVICE_LOGS_COMPOSE_CLI_ENV_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `awf service logs` preserves resolved
  `COMPOSE_PROJECT_NAME` and `COMPOSE_PROFILES` values when invoking
  `docker compose logs`.
- Complete: Preserved the safety contract that ordinary service secrets such as
  `AWF_API_TOKEN` and `AWF_DATABASE_URL` are not copied from the resolved
  service environment into the subprocess environment.
- Complete: Kept existing Docker host override and Compose interpolation
  behavior unchanged while adding Compose project/profile controls.
- Complete: Kept command construction, follow behavior, output handling, and
  structured failure behavior unchanged.
- Complete: Ran the focused regression, full service logs unit module, and
  ruff for touched source/test files, plus mypy for typed source.
- Complete: Prepared a scoped local commit without switching branches or
  pushing.

## Evidence

Changed files:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs.py`
- `plans/PRRT_kwDOSJAM6s6DOJTh_SERVICE_LOGS_COMPOSE_CLI_ENV_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DOJTh_SERVICE_LOGS_COMPOSE_CLI_ENV_VALIDATION.md`

Verification commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_preserves_compose_cli_vars_from_resolved_env -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py
uv run --python 3.12 --extra dev mypy src/awf
```

Results:

- Focused regression failed before implementation, then passed after the fix.
- Full service logs unit module: 24 passed.
- Ruff: passed.
- Mypy: passed.
