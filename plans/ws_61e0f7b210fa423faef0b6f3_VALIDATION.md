# Validation: ws_61e0f7b210fa423faef0b6f3

Plan reference: `plans/ws_61e0f7b210fa423faef0b6f3_PLAN.md`

## Iteration 1

- Requirement 1 (`out_of_scope_changes` + `provider_recovery` parity): Complete
- Requirement 2 (REST/MCP payload alignment): Complete
- Requirement 3 (CLI JSON validation before HTTP): Complete
- Requirement 4 (parity docs/status rows completed): Complete
- Requirement 5 (workspace-owned validation evidence): Complete

Evidence file: `docs/awf-plans/ws_61e0f7b210fa423faef0b6f3.validation.txt`

## Verification commands (status)
1. `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts/test_request_payload_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py -q` — PASS (323 passed).
2. `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/mcp/server.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts` — PASS.
3. `uv run --python 3.12 --extra dev mypy src/awf` — PASS.

Full command output, including statuses and timestamps, is recorded in the `.validation.txt` file.
