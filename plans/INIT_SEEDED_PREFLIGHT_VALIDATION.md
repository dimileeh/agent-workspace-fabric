# Init Seeded Preflight Validation

Plan reference: `plans/INIT_SEEDED_PREFLIGHT_PLAN.md`

## Requirement Status

- Complete: Seed the resolved env file before loading `local_service_environ`,
  constructing `Settings(_env_file=...)`, or running the Docker preflight.
  Evidence: `src/awf/cli/main.py` now calls `_seed_env_file` immediately after
  `_resolve_init_env_paths()` and before service env/settings resolution.
- Complete: Preserve `--no-write-env` behavior.
  Evidence: existing init suite, including the no-write-env regression, passes.
- Complete: Preserve pretty and JSON env seeding reporting without printing
  secret values.
  Evidence: existing init suite secret-redaction and env failure/reporting tests
  pass.
- Complete: Ensure bootstrap provider readiness continues to receive the same
  post-seed env view.
  Evidence: bootstrap still receives `service_env`; existing provider readiness
  bootstrap regression passes.
- Complete: Keep Docker failure handling and state directory creation ordering
  intact after the seeded env has been considered.
  Evidence: existing Docker failure regressions pass, and the state directory is
  still created only after the Docker diagnostic succeeds.

## Evidence

- Added failing-first regression:
  `tests/unit/cli/test_init.py::test_init_without_path_uses_seeded_compose_env_for_preflight`
  initially failed because preflight settings used `unix:///var/run/docker.sock`
  instead of the seeded `AWF_DOCKER_HOST`.
- Changed files:
  - `src/awf/cli/main.py`
  - `tests/unit/cli/test_init.py`
  - `plans/INIT_SEEDED_PREFLIGHT_PLAN.md`
  - `plans/INIT_SEEDED_PREFLIGHT_VALIDATION.md`
- Verification commands:
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_uses_seeded_compose_env_for_preflight -q`
    passed after implementation.
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
    passed: 56 tests.
  - `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
    passed.
  - `uv run --python 3.12 --extra dev mypy src/awf`
    passed.
