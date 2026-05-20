# Workspace Create Effort Parity Validation

Plan reference: `plans/WORKSPACE_CREATE_EFFORT_PARITY_PLAN.md`

## Requirement Status

- REST create schema accepts optional `task.effort`: Complete.
- Workspace create service stores `task.effort` in `task_policy["agent_effort"]`: Complete.
- CLI create help exposes `--effort`: Complete.
- CLI create posts `task.effort` when provided and omits it when not provided: Complete.
- MCP create schema exposes optional `effort`: Complete.
- MCP create hydrates the same canonical request model as REST when effort is provided: Complete.
- Contract metadata includes create effort for CLI and MCP: Complete.

## Evidence

Changed files:

- `src/awf/api/schemas.py`
- `src/awf/service/workspaces.py`
- `src/awf/cli/main.py`
- `src/awf/mcp/server.py`
- `tests/unit/api/test_workspaces.py`
- `tests/unit/cli/test_cli.py`
- `tests/unit/mcp/test_mcp_server.py`
- `tests/unit/contracts/test_request_payload_alignment.py`
- `tests/unit/contracts/_capabilities.py`
- `openapi.json`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts/test_request_payload_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_persists_agent_model_and_effort_override_in_task_policy -q
```

Result: `366 passed in 289.57s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py src/awf/cli/main.py src/awf/mcp/server.py src/awf/service/workspaces.py tests/unit/api/test_workspaces.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts/test_request_payload_alignment.py tests/unit/contracts/_capabilities.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

Result after regenerating `openapi.json`: `OK: openapi.json matches the current app spec.`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 156 source files`.

## Gaps

None.
