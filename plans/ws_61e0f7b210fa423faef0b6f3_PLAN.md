# Plan: ws_61e0f7b210fa423faef0b6f3

## Problem statement
Complete the remaining plan-conformance gap for this workspace: provide AWF-owned validation evidence for parity work on `Workspace create v2` task-policy fields.

## Scope
- Constrain work to the create-v2 policy-parity slice only.
- Do not change REST/CLI/MCP policy semantics; only verify and document current parity behavior.
- Preserve idempotency, provider readiness preflight, disk admission, and structured-error behavior.

## Requirements checklist
1. Ensure `Workspace create v2` MCP and CLI expose `out_of_scope_changes` and `provider_recovery`.
2. Ensure request payload shaping remains aligned between REST and MCP for those fields.
3. Ensure CLI parses malformed policy JSON locally before making HTTP request.
4. Ensure contract docs/status rows report implementation state as `MCP implemented` with backlog slice cleared.
5. Produce workspace-owned validation evidence in `docs/awf-plans/ws_61e0f7b210fa423faef0b6f3.validation.txt`.

## Implementation steps
1. Validate current code paths against requirements in existing files:
   - `src/awf/mcp/server.py`
   - `src/awf/cli/main.py`
   - `tests/unit/contracts/_capabilities.py`
   - `tests/unit/contracts/test_request_payload_alignment.py`
   - `tests/unit/contracts/test_surface_metadata_alignment.py`
   - `tests/unit/mcp/test_mcp_server.py`
   - `tests/unit/cli/test_cli.py`
   - `docs/MCP_CLIENT_PARITY.md`
   - `TODO/pre-gke-industrial-readiness.md`
2. Run required validation commands and capture output to:
   - `docs/awf-plans/ws_61e0f7b210fa423faef0b6f3.validation.txt`
3. Update validation status doc (`plans/ws_61e0f7b210fa423faef0b6f3_VALIDATION.md`) with per-requirement outcomes and evidence.

## Verification commands
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts/test_request_payload_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/mcp/server.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts`
- `uv run --python 3.12 --extra dev mypy src/awf`
