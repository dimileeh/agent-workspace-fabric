# Review Comment 4306484061 Plan

## Problem Statement and Scope

CodeRabbit flagged that the MCP `awf_create_workspace` tool silently prefers
legacy create fields (`branch_base`, `test_commands`) over canonical fields
(`base_branch`, `validation_commands`) when both forms are supplied with
different values. The fix should reject conflicting explicit aliases while
preserving existing canonical defaults and legacy-only compatibility.

## Requirements Checklist

- Add regression coverage for conflicting `base_branch`/`branch_base` inputs.
- Add regression coverage for conflicting `validation_commands`/`test_commands`
  inputs.
- Preserve legacy-only calls where `branch_base` or `test_commands` is supplied
  and the canonical field is omitted.
- Preserve canonical defaults: omitted base branch resolves to `main`, and
  omitted validation commands resolve to an empty list.
- Return a structured MCP `INVALID_REQUEST` error for alias conflicts.
- Keep the change scoped to MCP create argument handling and focused tests.

## Implementation Steps

1. Add failing unit tests in `tests/unit/mcp/test_mcp_server.py` for the two
   conflicting alias cases.
2. Update `awf_create_workspace` argument defaults so the implementation can
   distinguish omitted canonical fields from explicit canonical values.
3. Add explicit conflict guards before deriving effective create values.
4. Ensure effective defaults remain `main` and `[]` when both alias forms are
   absent.
5. Run the targeted MCP tests, then run the narrow lint/test validation needed
   for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server.py`
  passes.
