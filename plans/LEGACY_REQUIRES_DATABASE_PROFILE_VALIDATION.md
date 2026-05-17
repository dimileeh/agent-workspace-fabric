# Legacy Requires Database Profile Validation

Plan reference: `plans/LEGACY_REQUIRES_DATABASE_PROFILE_PLAN.md`

## Requirement Status

- Complete: Legacy `requires_database=true` maps to `profile_ref="aira"` in the
  flat request adapter.
- Complete: Legacy flat payloads without the database shortcut still use
  `env_profile` or `auto`.
- Complete: Rich request behavior remains unchanged; only the legacy flat
  coercion path changed.
- Complete: Added regression coverage that failed before the implementation
  change and passes afterward.
- Complete: Focused schema/service tests and Ruff validation pass.

## Evidence

- Changed `src/awf/api/schemas.py` to map the legacy database shortcut to the
  `aira` profile before building the canonical request body.
- Added
  `tests/unit/api/test_schema_coverage_edges.py::test_legacy_flat_workspace_create_requires_database_selects_aira_profile`.
- Updated
  `tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing`
  to assert the new mapped profile and use the existing provider readiness
  override pattern for the resolved `aira` profile.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_legacy_flat_workspace_create_requires_database_selects_aira_profile -q`
  failed before implementation with `request.workspace.profile_ref == "python"`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/service/test_workspaces_observability.py::test_workspace_service_create_v1_and_event_listing -q`
  passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py tests/unit/service/test_workspaces_observability.py`
  passed.

## Remaining Gaps

None.
