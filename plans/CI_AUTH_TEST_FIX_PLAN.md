# CI Auth Test Fix Plan

## Problem Statement And Scope

PR #250's `python-full-coverage` check fails in unit tests that seed or compare
protected workspace API surfaces without the configured test bearer token. The
focused local repro shows:

- `tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key`
  fails while creating a workspace because `POST /v1/workspaces` returns `503`
  `API_TOKEN_NOT_CONFIGURED`.
- `tests/unit/api/test_validation_provenance.py::test_validation_provenance_groups_streams_and_resolves_profile_commands`
  fails for the same protected workspace creation path.
- `tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_workspace_overview_tool_matches_rest_payload`
  fails because `GET /v1/workspaces/overview` returns `401` when the request
  omits the fixture's bearer token.

Scope is limited to restoring the test harness and affected test helpers so
they exercise the real protected endpoints with valid test auth.

## Requirements Checklist

- Keep REST auth enforcement intact; do not disable, skip, or weaken checks.
- Add valid test authorization to protected REST calls used by the failing
  tests.
- Keep changes scoped to test setup/helpers unless implementation inspection
  reveals a real product bug.
- Preserve strict idempotency and validation provenance behavior assertions.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Update affected API test helpers to use a shared test auth header before
   protected workspace creation and validation provenance reads.
2. Update MCP operator parity tests to pass `operator_stack.auth_headers` on
   REST calls that compare protected surfaces against MCP tool payloads.
3. Re-run the focused failing node IDs/files to confirm the original CI failures
   are resolved.
4. Run the narrow lint/type/test surface justified by the changed test files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key --maxfail=1 -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py::test_validation_provenance_groups_streams_and_resolves_profile_commands --maxfail=1 -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_workspace_overview_tool_matches_rest_payload --maxfail=1 -q`
  passes.
- If the first fixes expose adjacent auth omissions in the same files, run the
  full touched files and require them to pass.
