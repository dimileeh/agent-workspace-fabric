# Workspace Create Effort Parity Plan

## Problem

Workspace creation has model selection parity across REST, CLI, and MCP, but
reasoning effort selection is only exposed for PR monitor adoption. This leaves
`awf workspace create` unable to request a concrete effort such as `xhigh`, and
the MCP/API create contract does not make that field explicit either.

## Scope

- Add optional `task.effort` to canonical `POST /v1/workspaces`.
- Add `--effort` to `awf workspace create`.
- Add `effort` to `awf_create_workspace`.
- Persist create-time effort as `task_policy.agent_effort`.
- Add/update parity tests so REST/CLI/MCP create stay aligned.

## Requirements Checklist

- [ ] REST create schema accepts optional `task.effort` with non-empty bounded string validation.
- [ ] Workspace create service stores `task.effort` in `task_policy["agent_effort"]`.
- [ ] CLI create help exposes `--effort`.
- [ ] CLI create posts `task.effort` when provided and omits it when not provided.
- [ ] MCP create schema exposes optional `effort`.
- [ ] MCP create hydrates the same canonical request model as REST when effort is provided.
- [ ] Contract metadata includes create effort for CLI and MCP.

## Verification

Run focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli.py \
  tests/unit/mcp/test_mcp_server.py \
  tests/unit/contracts/test_request_payload_alignment.py \
  tests/unit/contracts/test_surface_metadata_alignment.py \
  -q
```

Run targeted lint:

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/api/schemas.py src/awf/cli/main.py src/awf/mcp/server.py \
  src/awf/service/workspaces.py \
  tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py \
  tests/unit/contracts/test_request_payload_alignment.py \
  tests/unit/contracts/_capabilities.py
```
