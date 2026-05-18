# MCP Legacy Branch Default Validation

Plan reference: `plans/MCP_LEGACY_BRANCH_DEFAULT_PLAN.md`

## Requirement Status

- Complete: Added a regression proving an MCP create call without branch
  arguments persists `development` as the workspace base branch.
- Complete: Updated the MCP create fallback so omitted branch input uses
  `development`.
- Complete: Updated and asserted the MCP tool schema/default description so it
  advertises the same `development` fallback.
- Complete: Preserved existing explicit alias behavior; focused create tests
  covering matching and conflicting aliases still pass.
- Complete: Prepared the local commit for review-thread fix tracking.

## Evidence

Files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server.py`
- `plans/MCP_LEGACY_BRANCH_DEFAULT_PLAN.md`
- `plans/MCP_LEGACY_BRANCH_DEFAULT_VALIDATION.md`

Verification:

- Failed first:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_omitted_branch_preserves_legacy_development_default -q`
  failed because the persisted branch was `main` instead of `development`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_omitted_branch_preserves_legacy_development_default -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_create_workspace_owned_paths_declares_item_constraints -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`

## Remaining Gaps

None.
