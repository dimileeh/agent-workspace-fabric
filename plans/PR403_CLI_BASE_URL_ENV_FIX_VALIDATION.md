# PR403 CLI Base URL Env Fix Validation

## Summary

Implemented the new PR review request to derive general CLI base URLs from the
same merged local Compose environment used for API token fallback. `_base_url()`
now resolves `AWF_API_HOST_PORT` from `local_service_environ(os.environ)` after
explicit base URL controls and shell `AWF_API_HOST_PORT`.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution -q`
  - Passed: 10 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py -q`
  - Passed: 22 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_control_surface_parity_contract.py::test_control_commands_emit_expected_request_shape_and_output -q`
  - Passed: 6 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/contracts/test_control_surface_parity_contract.py src/awf/cli/common.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_workspace_commands_helpers.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/contracts/test_control_surface_parity_contract.py src/awf/cli/common.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_workspace_commands_helpers.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest --splits 8 --group 2 --timeout=300 -q`
  - Passed: 1367 tests, 9569 deselected.
