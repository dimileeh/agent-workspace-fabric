# PRRT_kwDOSJAM6s6CkZpy Secret Leases Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CkZpy` reports that MCP workspace detail
payloads omit issued secret leases because `WorkspaceService.get()` loads
workspace operations but leaves the `secret_leases` relationship unloaded.
`workspace_response()` deliberately projects an empty list for unloaded secret
leases to avoid implicit lazy IO, so `awf_get_workspace` and
`awf_wait_for_workspace` can return `secret_leases: []` even when leases exist.

Scope is limited to the workspace detail fetch path used by MCP polling and the
corresponding regression coverage.

## Requirements Checklist

- Add a regression test proving MCP `awf_get_workspace` returns issued,
  sanitized secret lease status for a workspace.
- Preserve the existing no-lazy-load behavior in `workspace_response()`.
- Preserve operation preloading for `WorkspaceService.get()`.
- Keep list endpoints from eager-loading secret leases.
- Do not expose raw secret references in workspace payloads.

## Implementation Steps

1. Add a focused MCP regression that creates a workspace, issues a declared
   secret lease directly through the repository, and verifies
   `awf_get_workspace` includes the lease without leaking the raw ref.
2. Confirm the regression fails against the current service fetch path.
3. Change `WorkspaceService.get()` to use the existing repository detail fetch
   that preloads both operations and secret leases.
4. Run focused tests for the MCP regression and existing relationship-loading
   guard.
5. Write validation evidence to
   `plans/PRRT_kwDOSJAM6s6CkZpy_SECRET_LEASES_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestGetAndList::test_get_workspace_includes_issued_secret_leases -q`
  - Passes after the fix and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestRelationshipLoading::test_list_does_not_eager_load_secret_leases -q`
  - Passes, proving list behavior remains narrow.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces.py tests/unit/mcp/test_mcp_server.py::TestGetAndList -q`
  - Passes, proving nearby service/MCP workspace detail behavior.
