# PRRT_kwDOSJAM6s6CtYTq Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CtYTq_PLAN.md`

## Requirement Status

- Complete: Add a focused MCP regression test proving `requires_database=true`
  rejects a conflicting legacy `env_profile`.
  Evidence: `tests/unit/mcp/test_mcp_server.py` now includes
  `test_create_workspace_rejects_database_shortcut_conflicting_env_profile`.
  The test failed before implementation because the MCP call created a
  workspace instead of returning `INVALID_REQUEST`.
- Complete: Preserve valid legacy behavior where `requires_database=true` is
  combined with no profile selector, `profile_ref="auto"`, or an `aira`
  selector.
  Evidence: the existing legacy database shortcut test still passes, and the
  guard keeps `profile_ref in (None, "auto", "aira")` plus
  `env_profile in (None, "aira")` valid.
- Complete: Keep the change local to MCP request validation and avoid changing
  the REST compatibility schema behavior.
  Evidence: code changes are limited to `src/awf/mcp/server.py`; no API schema
  compatibility tests or behavior were changed.
- Complete: Return a structured `INVALID_REQUEST` error and avoid creating a
  workspace row.
  Evidence: the new regression asserts the exact `CallToolResult` structured
  error and verifies `WorkspaceRepository.list()` is empty.

## Verification Evidence

- Pre-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_env_profile -q`
  failed with `assert False is True` because the tool returned success.
- Post-fix focused check:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_env_profile -q`
  passed.
- Nearby MCP create checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_profile_ref tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_database_shortcut_conflicting_env_profile -q`
  passed: 3 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`
  passed.

## Gaps

None.
