# Collapse API Version And Clean Stale Docs Validation

## Summary

Status: Complete locally.

The implementation collapses workspace creation to one canonical rich
`POST /v1/workspaces` API, removes the public `/v2/workspaces` route, retires
the duplicate MCP create tool, updates CLI/MCP/contracts/docs, removes stale
operator scripts, and regenerates OpenAPI.

## Plan Checklist

- [x] Make the rich workspace create schema the public `WorkspaceCreateRequest`.
- [x] Route `POST /v1/workspaces` through the rich create/admission/preflight path.
- [x] Remove `router_v2` and stop registering `/v2/workspaces`.
- [x] Update CLI `awf workspace create` to post to `/v1/workspaces`.
- [x] Collapse MCP create into one rich `awf_create_workspace` tool.
- [x] Update contract/API/CLI/MCP tests away from `/v2/workspaces` and
      `awf_create_workspace_v2`.
- [x] Keep only generator scripts under `scripts/`; remove retired operational
      scripts/task JSON fixtures and stale script-only tests.
- [x] Update public docs to remove `/v2` create and legacy script guidance.
- [x] Regenerate OpenAPI and update validation notes.

## Validation

- Complete:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts tests/unit/docs -q`
  - Result: `877 passed in 431.88s`.
- Complete:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  - Result: `29 passed in 110.98s`.
- Complete:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py -q`
  - Result: `3 passed in 0.08s`.
- Complete:
  `uv run --python 3.12 --extra dev ruff check scripts src/awf tests`
  - Result: passed.
- Complete:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: `Success: no issues found in 155 source files`.
- Complete:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Result: `OK: openapi.json matches the current app spec.`

## Notes

- Plain system `python scripts/generate_openapi.py` is not used for validation
  in this checkout because project dependencies are loaded through `uv`.
- Historical plan/evidence docs were not rewritten beyond minimal labels and
  generated drift; public operator docs now describe only the canonical v1
  create surface.
