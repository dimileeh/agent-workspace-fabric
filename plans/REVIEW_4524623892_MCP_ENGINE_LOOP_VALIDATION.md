# Review 4524623892 MCP Engine Loop Validation

## Plan Reference

`plans/REVIEW_4524623892_MCP_ENGINE_LOOP_PLAN.md`

## Requirement Status

- Complete: `awf mcp serve` still reserves stdout for the MCP stdio transport
  and leaves Typer error handling in `mcp_serve`.
- Complete: `_run_mcp_server` now runs `server.run_stdio_async()` and
  `engine.dispose()` inside the same top-level `asyncio.run()` lifecycle.
- Complete: Engine disposal remains in a `finally` block, so it runs after
  server construction failures and after the stdio server returns.
- Complete: Focused MCP CLI tests now assert async stdio execution does not go
  through the sync wrapper and that server execution and disposal share the same
  running event loop.
- Complete: Only focused local checks were run. Full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_mcp_cli.py`
- `plans/REVIEW_4524623892_MCP_ENGINE_LOOP_PLAN.md`
- `plans/REVIEW_4524623892_MCP_ENGINE_LOOP_VALIDATION.md`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q`
  failed before implementation with the new regression because the old code
  called the sync MCP `run()` wrapper.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q`
  passed after implementation: `7 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_mcp_cli.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/cli/main.py`
  passed.
