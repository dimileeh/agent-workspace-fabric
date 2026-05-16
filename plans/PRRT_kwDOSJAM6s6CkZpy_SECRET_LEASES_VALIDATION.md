# PRRT_kwDOSJAM6s6CkZpy Secret Leases Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CkZpy_SECRET_LEASES_PLAN.md`

## Requirement Status

- Add a regression test proving MCP `awf_get_workspace` returns issued,
  sanitized secret lease status for a workspace: Complete.
  - Evidence: `tests/unit/mcp/test_mcp_server.py` adds
    `TestGetAndList::test_get_workspace_includes_issued_secret_leases`.
  - The test failed before the service fix with `IndexError: list index out of
    range` because `fetched["secret_leases"]` was empty.
- Preserve the existing no-lazy-load behavior in `workspace_response()`:
  Complete.
  - Evidence: `_loaded_secret_leases()` was not changed.
- Preserve operation preloading for `WorkspaceService.get()`: Complete.
  - Evidence: `src/awf/service/workspaces.py` now calls
    `WorkspaceRepository.get_with_secret_leases()`, whose query includes both
    `selectinload(Workspace.secret_leases)` and
    `selectinload(Workspace.operations)`.
- Keep list endpoints from eager-loading secret leases: Complete.
  - Evidence:
    `tests/unit/db/test_workspace_repository.py::TestRelationshipLoading::test_list_does_not_eager_load_secret_leases`
    passed.
- Do not expose raw secret references in workspace payloads: Complete.
  - Evidence: the new MCP regression asserts the raw profile ref string is not
    present in the serialized payload.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestGetAndList::test_get_workspace_includes_issued_secret_leases -q`
  - Before fix: failed as expected with empty `secret_leases`.
  - After fix: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestRelationshipLoading::test_list_does_not_eager_load_secret_leases -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces.py tests/unit/mcp/test_mcp_server.py::TestGetAndList -q`
  - Passed, `28 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/mcp/test_mcp_server.py`
  - Passed.

## Gaps

None.
