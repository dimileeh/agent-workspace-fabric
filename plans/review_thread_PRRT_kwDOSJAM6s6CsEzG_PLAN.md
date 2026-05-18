# PRRT_kwDOSJAM6s6CsEzG MCP Create Legacy Args Plan

## Problem Statement and Scope

The review thread reports that `awf_create_workspace` kept the public MCP tool
name while moving to the richer create signature, leaving older MCP callers that
send `branch_base`, `test_commands`, or `requires_database` without the legacy
mapping still preserved by REST and CLI.

Scope is limited to MCP workspace create compatibility, regression coverage, and
the required plan/validation artifacts for this review thread.

## Requirements Checklist

- [ ] Accept legacy MCP `branch_base` and map it to the effective base branch.
- [ ] Accept legacy MCP `test_commands` and map it to validation commands.
- [ ] Accept legacy MCP `requires_database=true` and map it to the legacy
      database profile selection.
- [ ] Preserve current canonical MCP arguments and defaults.
- [ ] Add focused regression tests showing the legacy arguments persist to the
      workspace row.
- [ ] Run the narrowest useful MCP test command and record evidence.

## Implementation Steps

1. Add a failing MCP regression test for legacy create arguments.
2. Implement compatibility mapping in `src/awf/mcp/server.py`.
3. Run the focused test, then any nearby MCP create tests needed by the change.
4. Create validation documentation against this plan.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q
```

Pass criteria: the new regression test passes and proves `branch_base`,
`test_commands`, and `requires_database` are honored through the MCP tool.
