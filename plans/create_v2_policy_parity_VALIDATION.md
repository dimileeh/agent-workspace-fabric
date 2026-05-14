# Validation: P1 create-v2 policy parity

## Validation focus
- Confirm MCP and CLI parity implementation for v2 task policy fields.
- Confirm canonical request alignment and parity-matrix consistency.
- Confirm docs/backlog accurately reflect implemented status.

## Test commands
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_request_payload_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q`

## Static checks
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/mcp/server.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py tests/unit/contracts`
- `uv run --python 3.12 --extra dev mypy src/awf`

## Acceptance
- `tests/unit/contracts/test_request_payload_alignment.py::test_mcp_create_v2_hydrates_canonical_request_model` includes both policy fields.
- `tests/unit/contracts/test_surface_metadata_alignment.py::test_create_v2_registry_status_tracks_mcp_payload_parity_gap` resolves to implemented status for v2.
- `tests/unit/cli/test_cli.py` includes positive and malformed JSON tests for both flags.
- `tests/unit/mcp/test_mcp_server.py` includes policy persistence and schema coverage for both fields.
