# Collapse API Version And Clean Stale Docs Plan

## Summary

Collapse AWF's public workspace creation surface to a single rich
`POST /v1/workspaces` contract while AWF is still pre-stable. Remove the
separate `/v2/workspaces` route and MCP `awf_create_workspace_v2` tool, update
CLI/MCP/contracts/docs to one canonical create surface, and retire legacy
operator scripts that are now superseded by CLI/API/MCP.

## Implementation Checklist

- [ ] Make the rich workspace create schema the public `WorkspaceCreateRequest`.
- [ ] Route `POST /v1/workspaces` through the rich create/admission/preflight path.
- [ ] Remove `router_v2` and stop registering `/v2/workspaces`.
- [ ] Update CLI `awf workspace create` to post to `/v1/workspaces`.
- [ ] Collapse MCP create into one rich `awf_create_workspace` tool.
- [ ] Update contract/API/CLI/MCP tests away from `/v2/workspaces` and
      `awf_create_workspace_v2`.
- [ ] Keep only generator scripts under `scripts/`; remove retired operational
      scripts/task JSON fixtures and stale script-only tests.
- [ ] Update public docs to remove `/v2` create and legacy script guidance.
- [ ] Regenerate OpenAPI and update validation notes.

## Validation Checklist

- [ ] Focused API/CLI/MCP/contract/doc tests pass.
- [ ] Ruff passes for touched Python.
- [ ] Mypy passes for `src/awf`.
- [ ] `python scripts/generate_openapi.py --check` passes.
