# PRRT_kwDOSJAM6s6CtYTq Plan

## Problem Statement and Scope

The MCP `awf_create_workspace` tool rejects `requires_database=true` when
`profile_ref` explicitly selects a conflicting non-`aira` profile, but it does
not reject the same conflict when the caller uses the legacy `env_profile`
alias. This lets a request send `env_profile="python"` and
`requires_database=true`, then silently launch with the `aira` profile.

## Requirements Checklist

- Add a focused MCP regression test proving `requires_database=true` rejects a
  conflicting legacy `env_profile`.
- Preserve valid legacy behavior where `requires_database=true` is combined with
  no profile selector, `profile_ref="auto"`, or an `aira` selector.
- Keep the change local to MCP request validation and avoid changing the REST
  compatibility schema behavior.
- Return a structured `INVALID_REQUEST` error and avoid creating a workspace row.

## Implementation Steps

1. Add a regression test in `tests/unit/mcp/test_mcp_server.py` beside the
   existing MCP create profile conflict tests.
2. Run that focused test before implementation and confirm it fails because the
   current MCP guard only checks `profile_ref`.
3. Update `src/awf/mcp/server.py` so the database shortcut conflict guard also
   rejects `env_profile` values outside `None` and `"aira"`.
4. Re-run the focused MCP create regression tests and lint the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_env_profile -q`
  fails before the implementation and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_profile_ref tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_env_profile -q`
  passes after the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`
  passes.
