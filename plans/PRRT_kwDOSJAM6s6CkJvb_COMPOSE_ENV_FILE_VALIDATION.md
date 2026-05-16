# PRRT_kwDOSJAM6s6CkJvb Compose Env File Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CkJvb_COMPOSE_ENV_FILE_PLAN.md`

## Requirement Status

- Complete: Add a regression proving bootstrap Compose stages pass
  `docker/compose/.env` when it exists.
  - Evidence: `tests/unit/service/test_bootstrap.py::test_bootstrap_passes_compose_env_file_when_available`
    failed before implementation because generated commands began with
    `docker compose -f ...`.
- Complete: Ensure all Compose bootstrap stages share the same env-file
  behavior.
  - Evidence: `src/awf/service/bootstrap.py` now routes stage command creation
    through `_compose_command()`.
- Complete: Do not add `--env-file` to the separate agent runtime image build.
  - Evidence: regression asserts the first `docker build` command does not
    contain `--env-file`.
- Complete: Keep existing bootstrap tests green.
  - Evidence: full bootstrap unit module passed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_passes_compose_env_file_when_available -q`
  - Before implementation: failed on missing `--env-file`.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q`
  - Passed: 16 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py tests/unit/service/test_bootstrap.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
