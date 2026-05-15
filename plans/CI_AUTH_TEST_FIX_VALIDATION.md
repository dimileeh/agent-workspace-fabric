# CI Auth Test Fix Validation

Plan reference: `plans/CI_AUTH_TEST_FIX_PLAN.md`

## Requirement Status

- Complete: REST auth enforcement remains intact. The fix sends valid test
  bearer tokens to protected endpoints instead of bypassing or disabling auth.
- Complete: Protected workspace creation and validation-provenance REST calls in
  the affected API tests now include test authorization.
- Complete: MCP operator parity REST comparisons now include the fixture's
  authorization headers for protected workspace surfaces.
- Complete: Idempotency, validation provenance, and MCP payload assertions are
  unchanged.
- Complete: Changes are limited to test helpers/parity requests plus the
  required plan and validation documents.

## Evidence

Files changed:

- `tests/unit/api/test_workspace_controls_idempotency.py`
- `tests/unit/api/test_validation_provenance.py`
- `tests/unit/mcp/test_mcp_operator_surfaces.py`
- `plans/CI_AUTH_TEST_FIX_PLAN.md`
- `plans/CI_AUTH_TEST_FIX_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key --maxfail=1 -q`
  passed: `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py::test_validation_provenance_groups_streams_and_resolves_profile_commands --maxfail=1 -q`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces.py::TestMcpOperatorSurfaceParity::test_workspace_overview_tool_matches_rest_payload --maxfail=1 -q`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py -q`
  passed: `61 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py -q`
  passed: `34 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_operator_surfaces.py -q`
  passed: `42 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_workspace_controls_idempotency.py tests/unit/api/test_validation_provenance.py tests/unit/mcp/test_mcp_operator_surfaces.py plans/CI_AUTH_TEST_FIX_PLAN.md`
  passed.

## Gaps

No gaps remain for the saved plan.
