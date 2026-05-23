# PR282 MCP Help Color Plan

## Problem Statement and Scope

PR #282 CI failed in `tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available`
because CI-style colorized Typer/Rich help output can split `--env-file` with
ANSI escape sequences. The option is present in the rendered help, but the raw
substring assertion is brittle.

Scope is limited to MCP CLI help tests. No product behavior, broad CI workflow,
or validation gate changes are planned.

## Requirements Checklist

- Preserve the `awf mcp serve --help` behavior and continue asserting the
  `--env-file` option is visible.
- Add focused regression coverage for CI-style forced-color help output.
- Keep local validation focused to the failing MCP CLI test module/node.
- Do not run broad AWF/GitHub-owned validation in the agent phase.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Add a regression test that invokes `awf mcp serve --help` with forced color
   environment variables and proves the raw assertion failure mode.
2. Normalize Typer/Rich help output with `click.unstyle` before checking visible
   MCP help text.
3. Re-run the focused MCP CLI tests and a targeted lint check for the touched
   test file.
4. Record validation evidence in `plans/PR282_MCP_HELP_COLOR_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_mcp_cli.py`
  passes.

Full AWF/GitHub validation remains owned by AWF after agent completion.
