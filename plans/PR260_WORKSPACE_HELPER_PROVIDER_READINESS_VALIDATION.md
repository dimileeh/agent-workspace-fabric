# PR260 Workspace Helper Provider Readiness Validation

Plan reference: `plans/PR260_WORKSPACE_HELPER_PROVIDER_READINESS_PLAN.md`

## Requirement status

- Reproduce the reported focused failures in an auth-sanitized environment:
  Complete.
  - With ambient Claude/Gemini auth removed and `HOME` pointed at an empty
    directory, the three REST nodes failed with `409` instead of `202` at
    `tests/unit/api/test_workspaces.py:463`.
- Preserve provider-readiness enforcement in production code: Complete.
  - Only the test-only `_create_workspace` fixture helper was changed.
  - The dedicated missing-provider-readiness blocking test still passes.
- Make `_create_workspace` fixture creates independent of ambient provider
  auth: Complete.
  - The helper now uses `_v2_body_with_preflight_override`, matching other
    fixture-only workspace creates.
- Keep direct tests unchanged unless a focused repro shows they still fail:
  Complete.
  - `tests/unit/api/test_workspaces_direct.py::TestListDirect::test_returns_rows`
    passed before and after the patch.
- Verify the sanitized focused repro passes after the patch: Complete.
- Write validation evidence and commit locally without pushing: Complete once
  this validation file is committed.

## Evidence

Commands run:

- `rm -rf /tmp/awf-noauth-home && mkdir -p /tmp/awf-noauth-home && env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN -u GEMINI_API_KEY -u GOOGLE_API_KEY -u GOOGLE_CLOUD_ACCESS_TOKEN HOME=/tmp/awf-noauth-home uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_legacy_v1_workspace_exposes_default_effective_identity tests/unit/api/test_workspaces.py::TestListWorkspaces::test_filters_by_agent tests/unit/api/test_workspaces.py::TestListWorkspaces::test_combines_filters tests/unit/api/test_workspaces_direct.py::TestListDirect::test_returns_rows -q`
  - Before fix: `3 failed, 1 passed`.
  - After fix: `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_legacy_v1_workspace_exposes_default_effective_identity tests/unit/api/test_workspaces.py::TestListWorkspaces::test_filters_by_agent tests/unit/api/test_workspaces.py::TestListWorkspaces::test_combines_filters tests/unit/api/test_workspaces_direct.py::TestListDirect::test_returns_rows -q`
  - Result: `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_create_blocks_missing_selected_provider_readiness -q`
  - Result: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestListWorkspaces tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata -q`
  - Result: `42 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_workspaces.py`
  - Result: passed.

## Gaps

None.
