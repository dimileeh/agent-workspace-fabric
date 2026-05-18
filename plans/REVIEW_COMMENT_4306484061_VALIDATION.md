# Review Comment 4306484061 Validation

Plan reference: `REVIEW_COMMENT_4306484061_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for conflicting `base_branch`/`branch_base`
  inputs.
- Complete: Added regression coverage for conflicting
  `validation_commands`/`test_commands` inputs.
- Complete: Preserved legacy-only `branch_base` and `test_commands`
  compatibility.
- Complete: Preserved effective defaults of `main` and `[]` for omitted
  canonical fields.
- Complete: Alias conflicts return structured MCP `INVALID_REQUEST` errors.
- Complete: Scope is limited to MCP create argument handling and focused tests.

## Evidence

Files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server.py`
- `plans/REVIEW_COMMENT_4306484061_PLAN.md`
- `plans/REVIEW_COMMENT_4306484061_VALIDATION.md`

Failing-before evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_conflicting_branch_aliases tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_conflicting_validation_command_aliases -q`
  failed because both mismatched alias cases returned successful workspace
  creates instead of `INVALID_REQUEST`.

Passing-after evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_conflicting_branch_aliases tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_rejects_conflicting_validation_command_aliases tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_matching_legacy_and_canonical_aliases tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
