# PR260 CI Provider Readiness Plan

## Problem statement and scope

PR #260 CI fails in `python-full-coverage` because several unit tests create
workspaces through REST or MCP without a provider-readiness override. In the CI
and AWF workspace environment, Docker may be unavailable, so the `codex` launch
probe blocks workspace creation before those tests can exercise idempotency,
artifact, or response-payload behavior.

Scope is limited to test setup/contract fixtures for tests whose subject is not
provider readiness. Existing provider-readiness block/override tests must keep
their coverage.

## Requirements checklist

- Reproduce the focused CI failures before coding.
- Preserve provider-readiness enforcement for production code and dedicated
  provider-readiness tests.
- Add explicit provider-readiness override intent only to unrelated create
  helpers/calls that need a workspace fixture.
- Verify the focused failing tests pass.
- Create validation evidence and commit the fix locally without pushing.

## Implementation steps

1. Patch `tests/unit/api/test_workspace_controls_idempotency.py` create payload
   to include an explicit test-only provider-readiness override.
2. Patch `tests/unit/api/test_artifacts.py` create payload similarly.
3. Patch `tests/unit/contracts/test_response_payload_alignment.py` MCP create
   contract call to pass the documented provider-readiness override fields.
4. Run the focused CI repro and the remaining reported failing node IDs.
5. Run a narrow provider-readiness blocking test to ensure enforcement was not
   disabled.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest '<reported node ids>' -q` passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_retry.py::test_retry_endpoint_blocks_missing_provider_readiness tests/unit/mcp/test_mcp_server.py::test_create_workspace_returns_structured_provider_preflight_error -q` passes.
