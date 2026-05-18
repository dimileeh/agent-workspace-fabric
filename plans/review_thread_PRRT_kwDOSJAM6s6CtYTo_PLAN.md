# Review Thread PRRT_kwDOSJAM6s6CtYTo Plan

## Problem Statement And Scope

The workspace creation service now routes legacy flat REST payloads and MCP
create calls through `create_workspace_row`. The helper persists
`requires_database=False`, so `WorkspaceResponse.requires_database` can report
`false` even when a legacy request set the database shortcut and
`WorkspaceCreateRequest.requires_database` is `true`.

Scope is limited to preserving the stored legacy response flag for create paths
that resolve to the legacy database profile through the existing request
contract.

## Requirements Checklist

- Add or update regression coverage proving a legacy database create response
  keeps `requires_database=True`.
- Preserve the canonical profile behavior that maps the legacy shortcut to the
  `aira` profile.
- Persist the request's effective legacy database flag in `create_workspace_row`.
- Do not broaden the change beyond workspace create compatibility.

## Implementation Steps

1. Update focused service/MCP tests to assert the legacy database flag survives
   create and fetch paths.
2. Run the focused test before the implementation change to confirm the current
   regression when practical.
3. Change `create_workspace_row` to store `payload.requires_database`.
4. Re-run the focused tests and a narrow related surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q`
- Pass criteria: both tests pass and assert the persisted response/database flag
  is `True` for legacy database shortcut requests.
