# MCP Legacy Branch Default Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Cs1V5` reports that `awf_create_workspace`
now falls back to `main` when neither `base_branch` nor `branch_base` is passed.
Legacy MCP callers, the CLI default, and the flat REST compatibility adapter
default omitted branch input to `development`. Preserve that legacy MCP behavior
without changing explicit `base_branch` or `branch_base` handling.

## Requirements Checklist

- Add a regression test proving an MCP create call without branch arguments
  persists `development` as the workspace base branch.
- Update the MCP create fallback so omitted branch input uses `development`.
- Keep MCP tool schema/default documentation aligned with the runtime fallback.
- Preserve existing alias conflict behavior for mismatched `base_branch` and
  `branch_base`.
- Commit the focused fix locally and print the AWF verdict.

## Implementation Steps

1. Add a focused unit regression under `tests/unit/mcp/test_mcp_server.py`.
2. Run the new regression to confirm it fails against the current fallback.
3. Update `src/awf/mcp/server.py` to use the legacy `development` default and
   adjust the field default hint/description.
4. Re-run the focused MCP tests that cover create defaults and aliases.
5. Create `plans/MCP_LEGACY_BRANCH_DEFAULT_VALIDATION.md` with requirement
   status and verification evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_omitted_branch_preserves_legacy_development_default -q`
  - First run should fail before implementation.
  - Final run should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace -q`
  - Final run should pass for the touched MCP create behavior.
