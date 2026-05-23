# MCP Setup Plan

## Goal

Make AWF's existing MCP tool surface installable by Claude Code and Codex via a
real packaged CLI entrypoint.

## Implementation

- Add `awf mcp serve` as a stdio MCP server command.
- Resolve local service settings from the default environment or an explicit
  `--env-file`.
- Build a database engine/session factory, construct `WorkspaceService`, call
  `build_mcp_server`, and run the server over stdio.
- Add MCP setup documentation with Claude Code and Codex snippets.
- Fix stale MCP docs that say create `effort` is excluded.

## Validation

- Add focused CLI tests for help, stdio launch, env-file resolution, and invalid
  env-file errors.
- Add docs tests for the Claude/Codex snippets and the effort-field correction.
- Run focused CLI/MCP tests, ruff, and mypy.
