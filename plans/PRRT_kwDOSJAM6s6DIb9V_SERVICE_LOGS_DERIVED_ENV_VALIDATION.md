# Validation: Preserve Derived Compose Env For Service Logs

Plan reference:
`plans/PRRT_kwDOSJAM6s6DIb9V_SERVICE_LOGS_DERIVED_ENV_PLAN.md`

## Requirement Status

- Complete: Added a regression proving a password derived by
  `local_service_environ()` is passed into the `docker compose logs`
  subprocess even without a Docker host override.
- Complete: Added coverage proving the resolved service environment overrides a
  stale caller `AWF_POSTGRES_PASSWORD` while still honoring `AWF_DOCKER_HOST` as
  subprocess `DOCKER_HOST`.
- Complete: Preserved the safety contract that service API tokens and database
  URLs from `service_environ` are not copied into the subprocess environment.
- Complete: Kept command construction, follow behavior, output handling, and
  structured failure behavior unchanged.
- Complete: Ran targeted service logs tests, the relevant CLI log-path tests,
  narrow lint, and a narrow mypy check.
- Complete: Scoped changes are ready to commit locally on the existing AWF
  branch.

## Evidence

Changed files:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs.py`
- `plans/PRRT_kwDOSJAM6s6DIb9V_SERVICE_LOGS_DERIVED_ENV_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DIb9V_SERVICE_LOGS_DERIVED_ENV_VALIDATION.md`

Failing-before evidence:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
```

Result before implementation: 2 failed, 18 passed. The new regressions failed
because the subprocess env was `None` without a Docker host and retained
`stale-secret` when a Docker host caused the helper to start from `os.environ`.

Verification commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_passes_existing_root_env_file_when_compose_env_is_missing tests/unit/cli/test_service_cli.py::test_service_logs_mirrors_compose_awf_docker_host_into_subprocess_env -q
```

Results:

- Service logs helper tests: 20 passed.
- Ruff: passed.
- Mypy: passed for `src/awf/service/logs.py`.
- Targeted CLI log-path tests: 2 passed.
