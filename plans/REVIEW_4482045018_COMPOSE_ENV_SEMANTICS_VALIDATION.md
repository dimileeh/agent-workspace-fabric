# Review 4482045018 Compose Env Semantics Validation

Plan reference:
`plans/REVIEW_4482045018_COMPOSE_ENV_SEMANTICS_PLAN.md`

## Requirement Status

- Complete: Added a service logs regression for a Compose interpolation variable
  other than `AWF_POSTGRES_PASSWORD`.
  Evidence:
  `tests/unit/service/test_logs.py::test_service_logs_passes_compose_interpolation_values_to_subprocess_env`.
- Complete: Service logs now derives interpolation variable names from the
  active Compose YAML and forwards only values Compose cannot otherwise resolve
  or values that must override a stale caller environment.
  Evidence: `src/awf/service/logs.py`.
- Complete: Preserved the service logs secret-suppression contract when an
  env file already supplies interpolation secrets.
  Evidence:
  `tests/unit/service/test_logs.py::test_service_logs_uses_env_file_instead_of_copying_interpolation_secrets`.
- Complete: Split the active env read source from the downstream Compose env
  file path for init, status, bootstrap, doctor, and readiness.
  Evidence: `src/awf/cli/main.py`.
- Complete: Updated source-checkout root `.env` fallback tests so root values
  still feed settings/provider env while downstream `compose_env_file` and
  bootstrap `env_file` parameters no longer receive the root fallback.
  Evidence: `tests/unit/cli/test_service_cli.py`.
- Complete: Focused and broader validation commands passed.
  Evidence listed below.

## Failing-Before Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_passes_compose_interpolation_values_to_subprocess_env tests/unit/cli/test_service_cli.py::test_service_logs_loads_existing_root_env_without_passing_it_as_compose_env_file tests/unit/cli/test_service_cli.py::test_service_status_resolves_settings_from_existing_root_env -q
```

Result before implementation: failed, `3 failed in 1.04s`. The dynamic logs
regression failed because the subprocess env was `None`; the status regression
failed because `compose_env_file` was the root `.env` fallback. The logs
root-fallback expectation was refined during implementation because `service
logs` directly maps that path to Docker Compose `--env-file`, unlike the
downstream status/bootstrap/doctor helper parameters.

## Verification Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
```

Result: passed, `22 passed in 0.64s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q
```

Result: passed, `74 passed in 5.92s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
```

Result: passed, `73 passed in 3.41s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/logs.py tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py
```

Result: passed, `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/cli/main.py src/awf/service/logs.py
```

Result: passed, `Success: no issues found in 2 source files`.

## Gaps

None for this review comment.
