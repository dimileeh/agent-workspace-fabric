# T09 MCP Setup Tools Plan

## Problem Statement And Scope

Implement T09 from `TODO/awf-full-installer-first-run-setup-backlog.md` by
exposing first-run setup/start/init/client capabilities through AWF's local MCP
server. The implementation contract is the AWF-supplied saved plan at
`docs/awf-plans/ws_c9cb06c77fbb47d38f3d774a.md`.

## Requirements Checklist

- Add MCP tools `awf_get_setup_status`, `awf_start_local_service`,
  `awf_initialize_project_profile`, and
  `awf_get_client_integration_instructions`.
- Reuse existing setup/start/init/client service functions and CLI helpers.
- Keep raw credential values out of MCP inputs and responses.
- Return setup status as safe refs/status metadata only.
- Make MCP start repeatable and return structured first-run failures.
- Make MCP project initialization use the same onboarding writer as the CLI.
- Return client instructions without env-file contents or secret values.
- Update MCP reference/parity docs and focused parity tests.

## Implementation Steps

1. Add focused failing MCP setup tool tests.
2. Add `src/awf/mcp/setup_tools.py` with bounded tool schemas and pure payload
   helpers.
3. Register setup tools from `src/awf/mcp/server.py`.
4. Update MCP reference/parity docs and any guarded docs tests.
5. Run focused MCP tests, focused parity tests, focused ruff, and focused mypy.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/mcp/server.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/mcp/server.py
```

Full AWF/GitHub validation and coverage gates are intentionally left to AWF
after the agent phase.
