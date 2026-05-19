# Release Readiness Compose Env Validation

Plan reference: `PRRT_kwDOSJAM6s6DGh_i_PLAN.md`

## Requirement Status

- Complete: `awf service readiness` now resolves `ServiceSettings` from the
  active local-service env file, matching `service status` and `service doctor`.
- Complete: The readiness collector receives the merged service env as both
  `provider_environ` and `environ`.
- Complete: The release-readiness collector forwards the resolved Compose file
  and env file to doctor diagnostics.
- Complete: Existing strict-provider validation, output, alias, and failure
  exit behavior remain covered by existing service CLI tests.
- Complete: Added a regression test proving Compose-only readiness settings are
  honored from a source-checkout subdirectory.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `src/awf/service/readiness.py`
- `tests/unit/cli/test_service_cli.py`
- `plans/PRRT_kwDOSJAM6s6DGh_i_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DGh_i_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_readiness_resolves_settings_from_compose_env -q`
  - Failed before implementation with the default local DB URL instead of the
    Compose-only URL.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_readiness_resolves_settings_from_compose_env -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  - Passed: 68 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py -q`
  - Passed: 31 tests.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/readiness.py tests/unit/cli/test_service_cli.py`
  - Initially found import-order issues in the new tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/readiness.py tests/unit/cli/test_service_cli.py`
  - Passed after import cleanup.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/service/test_readiness.py -q`
  - Passed: 99 tests.

## Gaps

None.
