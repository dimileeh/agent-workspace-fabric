# PR403 Legacy Env Migration Guard Validation

## Result

Implemented the migration guard from the plan.

- `_migrate_legacy_service_env_file()` now only migrates legacy
  `docker/compose/.env` values when the canonical `.env` belongs to an
  AWF source-shaped root.
- Verified source-root tests still cover migration from legacy Compose env into
  root `.env`.
- Non-source project tests now verify that a project-local
  `docker/compose/.env` is ignored and left intact.
- Init source-checkout tests now declare explicit AWF source markers before
  relying on legacy env migration or migrated state-dir resolution.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py::test_service_bootstrap_cli_migrates_legacy_compose_env tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py::test_service_bootstrap_cli_does_not_migrate_legacy_env_from_current_project -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_reports_legacy_env_migration_without_values tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_reports_legacy_env_migration_without_values -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_without_path_uses_compose_env_host_work_dir_for_state_directory -q`
  - Passed: 1 test.
- `uv run --python 3.12 --extra dev pytest --splits 8 --group 2 --timeout=300 -q`
  - Passed: 1367 tests, 9569 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/init_ops.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_004.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/cli/init_ops.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_004.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

No known implementation gaps for this review-thread fix. Full GitHub CI
continues on the PR after push.
