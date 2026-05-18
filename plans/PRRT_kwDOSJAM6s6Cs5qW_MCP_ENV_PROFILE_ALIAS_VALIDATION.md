# MCP env_profile Alias Review Fix Validation

Plan reference: `PRRT_kwDOSJAM6s6Cs5qW_MCP_ENV_PROFILE_ALIAS_PLAN.md`

## Requirement Status

- Add a regression test showing an MCP `env_profile` argument persists as the
  workspace `profile_ref`: Complete.
- Add MCP tool handling that maps `env_profile` to `profile_ref`: Complete.
- Reject conflicting explicit `profile_ref` and `env_profile` values instead of
  guessing: Complete.
- Verify the focused MCP test surface passes: Complete.

## Evidence

Files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server.py`
- `plans/PRRT_kwDOSJAM6s6Cs5qW_MCP_ENV_PROFILE_ALIAS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Cs5qW_MCP_ENV_PROFILE_ALIAS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py -q -k "env_profile or conflicting_profile_aliases"`
  - Initial result before implementation: failed because `env_profile` was
    ignored and conflicting aliases still created a workspace.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py -q -k "env_profile or conflicting_profile_aliases"`
  - Final result: `2 passed, 100 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py -q -k "owned_paths_declares_item_constraints or legacy_flat_arguments or env_profile or profile_aliases"`
  - Final result: `4 passed, 98 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`
  - Final result: `All checks passed!`

## Remaining Gaps

None.
