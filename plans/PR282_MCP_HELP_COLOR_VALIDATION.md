# PR282 MCP Help Color Validation

Plan reference: `plans/PR282_MCP_HELP_COLOR_PLAN.md`

## Requirement Status

- Preserve `awf mcp serve --help` behavior and continue asserting `--env-file`
  is visible: Complete.
- Add focused regression coverage for CI-style forced-color help output:
  Complete.
- Keep local validation focused to the failing MCP CLI test module/node:
  Complete.
- Do not run broad AWF/GitHub-owned validation in the agent phase: Complete.
- Commit the fix locally with a conventional commit message: Complete once this
  validation record and the test fix are committed together.

## Evidence

Files changed:

- `tests/unit/cli/test_mcp_cli.py`
- `plans/PR282_MCP_HELP_COLOR_PLAN.md`
- `plans/PR282_MCP_HELP_COLOR_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available -q`
  passed before edits in this workspace, showing the single-node repro only
  fails when color is forced.
- `uv run --python 3.12 --extra dev python - <<'PY' ... PY` with CI-style
  color env showed raw output did not contain `--env-file` while ANSI styling
  was present.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available_when_color_is_forced -q`
  failed before the assertion fix with `AssertionError: assert '--env-file' in ...`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available tests/unit/cli/test_mcp_cli.py::test_mcp_serve_help_is_available_when_color_is_forced -q`
  passed after the assertion fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q`
  passed: 7 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_mcp_cli.py`
  passed.

Full AWF/GitHub validation was not run locally; AWF owns broad validation after
agent completion.
