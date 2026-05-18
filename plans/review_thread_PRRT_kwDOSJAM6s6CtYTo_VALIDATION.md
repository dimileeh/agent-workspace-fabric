# Review Thread PRRT_kwDOSJAM6s6CtYTo Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CtYTo_PLAN.md`

## Requirement Status

- Add or update regression coverage proving a legacy database create response
  keeps `requires_database=True`: Complete. Updated the service create
  observability test to assert both create and fetched responses, and updated
  the MCP legacy create test to assert the stored row.
- Preserve the canonical profile behavior that maps the legacy shortcut to the
  `aira` profile: Complete. Existing assertions still verify `env_profile` /
  `profile_ref` resolve to `aira`.
- Persist the request's effective legacy database flag in `create_workspace_row`:
  Complete. `create_workspace_row` now passes `payload.requires_database` to
  `WorkspaceRepository.create`.
- Do not broaden the change beyond workspace create compatibility: Complete.
  Production change is limited to the stored legacy flag in the shared create
  helper.

## Evidence

- Changed `src/awf/service/workspaces.py`.
- Changed `tests/unit/service/test_workspaces_observability.py`.
- Changed `tests/unit/mcp/test_mcp_server.py`.

## Commands Run

- Expected failure before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q`
  failed with both assertions observing `requires_database=False`.
- Focused pass after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace::test_create_workspace_accepts_legacy_flat_arguments -q`
  passed.
- Related surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability.py tests/unit/mcp/test_mcp_server.py::TestCreateWorkspace tests/unit/service/test_workspace_idempotency.py::test_create_database_profile_replays_legacy_requires_database_row -q`
  passed with 128 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspaces_observability.py tests/unit/mcp/test_mcp_server.py`
  passed.

## Gaps

None.
