# MCP Setup Validation

## Result

Implemented the planned MCP setup slice.

## Evidence

- Added `awf mcp serve` as a stdio MCP server command.
- Added `docs/MCP_SETUP.md` with Claude Code and Codex setup snippets.
- Linked MCP setup from README, docs index, MCP reference, and CLI reference.
- Corrected stale docs that claimed `effort` was excluded from CLI/MCP create.
- Added focused CLI and MCP docs tests.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py tests/unit/mcp/test_mcp_client_parity_docs.py -q`
  - Result: `21 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli tests/unit/mcp -q`
  - Result: `600 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed

## Notes

This slice intentionally does not implement guided `awf init <repo>` onboarding
or Homebrew release packaging. Those remain separate product slices.
