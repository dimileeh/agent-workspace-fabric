# Review 4524623892 MCP Engine Loop Plan

## Problem Statement And Scope

PR review comment `issue:4524623892` flags that `awf mcp serve` currently runs
the MCP stdio server in one async event loop and disposes the SQLAlchemy async
engine in a second `asyncio.run()` loop. If the MCP session opens asyncpg-backed
connections, cross-loop disposal can warn or leak pooled connections.

Scope is limited to the packaged MCP CLI lifecycle and its focused unit tests.

## Requirements Checklist

- Keep `awf mcp serve` stdout reserved for MCP stdio transport and preserve
  existing Typer error handling.
- Run the MCP stdio server coroutine and async engine disposal in the same
  top-level event loop.
- Dispose the engine when server construction fails and when the stdio server
  returns.
- Add or update focused regression coverage for same-loop server execution and
  disposal.
- Do not run broad AWF/GitHub-owned validation inside the agent phase.

## Implementation Steps

1. Update `tests/unit/cli/test_mcp_cli.py` so the happy-path MCP CLI tests
   exercise async stdio execution and assert engine disposal occurs on the same
   event loop.
2. Run the focused MCP CLI tests to confirm the current implementation fails the
   new regression.
3. Update `src/awf/cli/main.py` to create the engine inside a top-level async
   helper, await `server.run_stdio_async()`, and await `engine.dispose()` in the
   same helper.
4. Re-run the focused MCP CLI tests and a narrow linter command for touched
   files.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q`
  passes after the implementation and fails on the new same-loop assertion
  before it.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_mcp_cli.py`
  passes.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
