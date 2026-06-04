# PR403 Shard 2 CLI Header Fix Validation

## Summary

Updated the stale workspace CLI helper assertion exposed by GitHub coverage
shard 2. The minimal `workspace_create` helper test now expects the local
Compose default `Authorization` header when no explicit API token is supplied,
matching the fixed CLI behavior introduced for fresh root Compose stacks.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_minimal_development_payload -q`
  - Passed: 1 test.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_workspace_commands_helpers.py -q`
  - Passed: 11 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_workspace_commands_helpers.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/cli/test_workspace_commands_helpers.py`
  - Passed.

- `uv run --python 3.12 --extra dev pytest --splits 8 --group 2 --timeout=300 -q`
  - Passed: 1367 tests, 9568 deselected.
