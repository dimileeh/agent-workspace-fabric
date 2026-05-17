# PRRT_kwDOSJAM6s6CsEzG MCP Create Legacy Args Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CsEzG_PLAN.md`

## Requirement Status

- Complete: Accept legacy MCP `branch_base` and map it to the effective base
  branch.
  Evidence: `src/awf/mcp/server.py` computes `effective_base_branch` from
  `branch_base` before `base_branch`; the regression asserts the persisted row
  stores `legacy-base`.
- Complete: Accept legacy MCP `test_commands` and map it to validation
  commands.
  Evidence: `src/awf/mcp/server.py` computes `effective_validation_commands`;
  the regression asserts `ws.test_commands` stores the legacy command list.
- Complete: Accept legacy MCP `requires_database=true` and map it to the legacy
  database profile selection.
  Evidence: `src/awf/mcp/server.py` maps true to `profile_ref="aira"`; the
  regression asserts the persisted row uses the `aira` profile.
- Complete: Preserve current canonical MCP arguments and defaults.
  Evidence: the full `TestCreateWorkspace` MCP test class still passes.
- Complete: Add focused regression tests showing legacy arguments persist to the
  workspace row.
  Evidence:
  `tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments`.
- Complete: Run the narrowest useful MCP test command and record evidence.
  Evidence: see commands below.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q
```

Result: passed after implementation. Before implementation, this failed because
`ws.branch_base` was `main` instead of `legacy-base`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace -q
```

Result: passed, `15 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py::test_mcp_tool_schema_matches_registry tests/unit/contracts/test_surface_metadata_alignment.py::test_create_registry_status_tracks_mcp_payload_parity_gap -q
```

Result: passed, `39 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py
```

Result: passed.
