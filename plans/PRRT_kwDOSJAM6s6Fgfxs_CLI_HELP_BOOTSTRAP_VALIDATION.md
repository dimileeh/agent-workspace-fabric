# PRRT_kwDOSJAM6s6Fgfxs CLI Help Bootstrap Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Fgfxs_CLI_HELP_BOOTSTRAP_PLAN.md`

## Requirement Status

- Top-level `awf --help` recommends `awf service bootstrap`, then
  `awf init <path>`: Complete.
- `awf init --help` recommends `awf service bootstrap`, then
  `awf init <path>`: Complete.
- Other shared first-path help snippets do not recommend placeholder setup/start
  as the current first path: Complete.
- Placeholder command behavior for `awf setup` and `awf start` remains unchanged:
  Complete. The commands still exit with their placeholder reason codes; their
  next steps now point at the current runnable service bootstrap path.
- Add/update focused regression tests before implementation: Complete.
- Run only targeted validation: Complete.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `src/awf/cli/service_commands.py`
- `src/awf/cli/setup_commands.py`
- `src/awf/cli/start_commands.py`
- `src/awf/cli/workspace_commands.py`
- `tests/unit/cli/test_cli_parts/test_cli_part_002.py`
- `tests/unit/cli/test_init_parts/test_init_part_001.py`
- `tests/unit/cli/test_init_parts/test_init_part_004.py`
- `tests/unit/cli/test_setup_commands.py`
- `tests/unit/cli/test_start_commands.py`

Focused TDD failure before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestCliHelp -q`
- Result: failed as expected with 4 failures because help did not contain
  `current runnable first path` / `awf service bootstrap`.

Focused validation after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestCliHelp tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_returns_migration_error tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_returns_migration_payload tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_legacy_bootstrap_flags_with_migration tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_with_path_rejects_bootstrap_only_flags_with_clear_error tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_with_path_rejects_no_write_env_flag tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py -q`
- Result: 25 passed.

Focused lint:

- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/cli/service_commands.py src/awf/cli/workspace_commands.py src/awf/cli/setup_commands.py src/awf/cli/start_commands.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_004.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py`
- Result: all checks passed.

Full AWF/GitHub validation was not run in this agent phase per the workspace
contract; AWF owns broad validation after completion.
