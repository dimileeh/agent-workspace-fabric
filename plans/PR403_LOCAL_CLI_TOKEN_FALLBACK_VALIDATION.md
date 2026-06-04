# PR403 Local CLI Token Fallback Validation

## Result

Implemented a narrow local Compose token fallback for CLI HTTP calls.

- `_api_token_headers()` remains explicit-only (`--api-token` or shell
  `AWF_API_TOKEN`).
- `_call()` adds the local Compose token only when the resolved API target is a
  loopback URL and no `Authorization` header is already present.
- Explicit remote `--base-url` targets do not receive `local-dev-token`.

## Validation

Initially failed as expected:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestWorkspaceObservability::test_runtime_uses_local_compose_token_for_default_local_target tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestWorkspaceObservability::test_runtime_does_not_send_local_compose_token_to_explicit_remote_target -q
```

Passed after the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_service_gc_cli.py -q
```

Passed:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/common.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_service_gc_cli.py
uv run --python 3.12 --extra dev ruff format --check src/awf/cli/common.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_service_gc_cli.py
uv run --python 3.12 --extra dev mypy src/awf
```
