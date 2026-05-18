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

## Gaps

None.
