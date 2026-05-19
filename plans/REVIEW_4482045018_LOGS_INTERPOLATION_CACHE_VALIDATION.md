# Review 4482045018 Logs Interpolation Cache Validation

Plan reference: `plans/REVIEW_4482045018_LOGS_INTERPOLATION_CACHE_PLAN.md`

## Requirement Status

- Complete: Added a regression proving repeated `awf service logs` calls against
  an unchanged Compose file do not repeatedly parse the YAML.
  Evidence: `tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes`.
- Complete: Kept the existing regression proving edits to the same Compose file
  path are observed by later logs calls.
  Evidence: `tests/unit/service/test_logs.py::test_service_logs_reloads_compose_interpolation_keys_when_file_changes`
  passed with the new cache in place.
- Complete: Cached Compose interpolation key discovery with invalidation when
  the file metadata changes.
  Evidence: `src/awf/service/logs.py` now uses an `lru_cache` helper keyed by
  resolved path, mtime nanoseconds, and file size.
- Complete: Added an explanatory comment documenting why equal env-file values
  can be omitted from the explicit subprocess env.
  Evidence: `_compose_interpolation_environ` now documents the
  `_docker_cli_environ` base-env and `--env-file` invariant.
- Complete: Verified the state-directory concern is already satisfied by
  current behavior.
  Evidence: `tests/unit/cli/test_init.py::test_init_without_path_uses_compose_env_host_work_dir_for_state_directory`
  passed, proving the compose-env state directory is created and printed.
- Complete: Ran focused logs tests and static checks for the touched files.
  Evidence: commands listed below passed.
- Complete: Commit scope is limited to this review fix cycle's source, tests,
  and required plan docs.
  Evidence: final staging will include only the files changed for this cycle.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes -q`
  - Failed as expected before implementation: `assert 2 == 1`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes tests/unit/service/test_logs.py::test_service_logs_reloads_compose_interpolation_keys_when_file_changes -q`
  - Passed: `2 passed in 0.75s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
  - Passed: `27 passed in 1.10s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_uses_compose_env_host_work_dir_for_state_directory -q`
  - Passed: `1 passed in 1.20s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/logs.py tests/unit/service/test_logs.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
