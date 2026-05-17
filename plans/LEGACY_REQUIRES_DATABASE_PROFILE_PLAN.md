# Legacy Requires Database Profile Plan

## Problem Statement And Scope

Legacy flat `POST /v1/workspaces` payloads still accept `requires_database`.
The compatibility coercion currently drops that flag while selecting
`env_profile` or `auto`, which can launch a workspace without the database
profile the caller requested.

Scope is limited to the legacy flat request adapter and focused regression
coverage. The canonical rich request shape remains profile-driven and must keep
persisting `requires_database=False`.

## Requirements Checklist

- Map legacy `requires_database=true` to the built-in `aira` profile, matching
  the CLI `--with-db` compatibility shortcut.
- Preserve legacy flat payload compatibility for callers that do not request a
  database.
- Keep canonical rich request behavior unchanged.
- Add a regression test that fails against the current silent-ignore behavior.
- Run focused unit validation for the changed schema/service surface.

## Implementation Steps

1. Add a schema regression for a legacy flat payload with
   `requires_database=true`.
2. Confirm the regression fails before changing implementation.
3. Update `WorkspaceCreateRequest._coerce_legacy_flat_payload` to select
   `profile_ref="aira"` when the legacy database flag is enabled.
4. Update any existing tests that intentionally cover the legacy compatibility
   path so they assert the new profile mapping instead of the silent ignore.
5. Run focused unit tests and lint on touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py tests/unit/service/test_workspaces_observability.py`
  must pass.
