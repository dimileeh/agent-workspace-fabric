# PR260 CI Provider Readiness Validation

Plan reference: `plans/PR260_CI_PROVIDER_READINESS_PLAN.md`

## Requirement status

- Reproduce the focused CI failures before coding: Complete.
  - Focused repro command failed with five setup failures where
    `/v1/workspaces` returned `409` instead of `202`.
  - Direct API repro showed `PROVIDER_READINESS_PRECHECK_FAILED` with
    `CODEX_RUNTIME_CLI_NOT_FOUND` because Docker was unavailable.
- Preserve provider-readiness enforcement for production code and dedicated
  provider-readiness tests: Complete.
  - Only test fixture create payloads/calls were changed; production
    provider-readiness code was not modified.
  - Dedicated block tests still pass.
- Add explicit provider-readiness override intent only to unrelated create
  helpers/calls that need a workspace fixture: Complete.
  - `tests/unit/api/test_workspace_controls_idempotency.py`
  - `tests/unit/api/test_artifacts.py`
  - `tests/unit/contracts/test_response_payload_alignment.py`
  - `tests/unit/api/test_events.py`
  - `tests/unit/api/test_observability_api.py`
  - `tests/unit/api/test_pagination_envelopes.py`
  - `tests/unit/api/test_workspaces_direct.py`
  - `tests/unit/contracts/test_structured_error_envelope.py`
- Verify the focused failing tests pass: Complete.
- Create validation evidence and commit the fix locally without pushing:
  Complete once this validation file is committed.

## Evidence

Commands run:

- `uv run --python 3.12 --extra dev pytest 'tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key[cancel]' 'tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key[stop]' 'tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_require_idempotency_key[destroy]' 'tests/unit/api/test_workspace_controls_idempotency.py::test_replay_same_key_returns_same_operation_without_duplicate_rows[cancel]' 'tests/unit/api/test_workspace_controls_idempotency.py::test_replay_same_key_returns_same_operation_without_duplicate_rows[stop]' -q`
  - Result: `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py tests/unit/contracts/test_response_payload_alignment.py::test_create_registry_response_fields_match_mcp_payload tests/unit/api/test_artifacts.py::TestWorkspaceArtifacts::test_requires_local_bearer_token_when_configured tests/unit/api/test_artifacts.py::TestWorkspaceArtifacts::test_download_requires_local_bearer_token_when_configured tests/unit/api/test_artifacts.py::TestWorkspaceArtifacts::test_existing_workspace_without_artifact_directory_returns_empty_list tests/unit/api/test_artifacts.py::TestWorkspaceArtifacts::test_lists_recursive_file_metadata_and_skips_escaping_symlinks -q`
  - Result: `74 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_retry.py::test_retry_endpoint_blocks_missing_provider_readiness -q`
  - Result: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_returns_structured_provider_preflight_error -q`
  - Result: `1 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_workspace_controls_idempotency.py tests/unit/api/test_artifacts.py tests/unit/contracts/test_response_payload_alignment.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_events.py tests/unit/api/test_observability_api.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_workspaces_direct.py tests/unit/contracts/test_structured_error_envelope.py -q`
  - First iteration result: failed in
    `test_websocket_stream_includes_monitor_and_recovery_logs` because the
    direct repository seed reused the REST-only `preflight` key.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket::test_websocket_stream_includes_monitor_and_recovery_logs tests/unit/api/test_events.py tests/unit/api/test_observability_api.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_workspaces_direct.py tests/unit/contracts/test_structured_error_envelope.py -q`
  - Result: `87 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_retry.py::test_retry_endpoint_blocks_missing_provider_readiness tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_returns_structured_provider_preflight_error -q`
  - Result: `2 passed`.
- `CI=true uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`
  - First iteration result: failed with 33 provider-readiness precheck fixture
    failures in additional API/direct create helpers.
  - Final result: `6528 passed, 7 skipped`; coverage `99.00%`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_events.py tests/unit/api/test_observability_api.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_workspaces_direct.py tests/unit/contracts/test_structured_error_envelope.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/api/test_events.py tests/unit/api/test_observability_api.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_workspaces_direct.py tests/unit/contracts/test_structured_error_envelope.py`
  - Result: passed.

## Iteration 2

The first local full-coverage run after the initial focused fix found the same
provider-readiness precheck failure in broader fixture-only workspace create
helpers. The follow-up patch added explicit provider-readiness overrides to
those helpers and kept direct repository seeds free of REST-only `preflight`
payloads. The final local full-coverage run passed.

## Gaps

None.
