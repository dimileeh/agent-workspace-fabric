# PRRT_kwDOSJAM6s6DB2ty Validation

Plan reference: `PRRT_kwDOSJAM6s6DB2ty_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing a cwd `docker/compose/.env` value
  does not leak into bootstrap when assets resolve to another checkout.
- Complete: Preserved the existing partial `provider_environ` overlay behavior;
  the existing bootstrap test for that behavior still passes.
- Complete: Bootstrap now loads its base environment from the resolved compose
  env location before applying provider overrides.
- Complete: Ran the narrow unit and static validation surface.

## Evidence

Files changed:

- `src/awf/service/bootstrap.py`
- `tests/unit/service/test_bootstrap.py`
- `plans/PRRT_kwDOSJAM6s6DB2ty_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DB2ty_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_does_not_overlay_provider_environment_on_cwd_compose_env -q`
  - Failed before the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_does_not_overlay_provider_environment_on_cwd_compose_env -q`
  - Passed after the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q`
  - Passed: 25 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  - Passed: 60 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py tests/unit/service/test_bootstrap.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/bootstrap.py`
  - Passed.
