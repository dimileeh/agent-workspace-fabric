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

## Review Repair: PRRT_kwDOSJAM6s6G_-HK

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` diverges from
`awf setup --dry-run --source-checkout` by reading host setup config after
`_run_setup` even when an explicit `source_checkout` was provided. The CLI
intentionally skips that read on explicit-checkout dry-runs so a corrupt or
secret-bearing `~/.awf/config.yml` cannot abort a read-only checkout probe.

Scope is limited to MCP setup status parity and its focused regression test.

### Requirements Checklist

- Preserve normal `awf_get_setup_status` behavior when `source_checkout` is not
  provided, including safe persisted provider/client/config metadata.
- For explicit `source_checkout`, do not call `read_host_setup_config()` after
  `_run_setup`.
- Keep the MCP response schema stable and secret-free on the explicit-checkout
  path.
- Add a regression proving a corrupt host config cannot turn an explicit
  checkout status probe into an MCP error.

### Implementation Steps

1. Add the focused failing MCP regression for explicit source-checkout status.
2. Change `_get_setup_status_result` to use an empty in-memory
   `HostSetupConfig` when `source_checkout` is explicit, matching the CLI's
   dry-run config-read skip while preserving response keys.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6G_-HM

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` returns a normal MCP tool
result when `_run_setup` returns a `blocked` or `failed` first-run readiness
payload without raising. That makes host readiness blockers look successful at
the MCP protocol layer unless clients inspect the payload status.

Scope is limited to MCP setup status `isError` parity and its focused regression
test.

### Requirements Checklist

- Preserve successful and warning setup status responses as non-error MCP tool
  calls.
- Mark `awf_get_setup_status` responses as MCP errors when rendered readiness
  status is `blocked` or `failed`.
- Keep the existing setup status response payload shape and redaction behavior.
- Add a focused regression proving blocked and failed readiness payloads set
  `result.isError`.

### Implementation Steps

1. Add the focused failing MCP regression for blocked/failed setup status.
2. Change `_get_setup_status_result` to pass `is_error=True` only when rendered
   setup readiness status is `blocked` or `failed`.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.
