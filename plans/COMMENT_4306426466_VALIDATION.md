# Comment 4306426466 Validation

Plan reference: `COMMENT_4306426466_PLAN.md`

## Requirement Status

- Add a regression test that fails if an external-id conflict persists the
  rejected workspace row: Complete.
- Keep the existing structured `409 TASK_EXTERNAL_ID_CONFLICT` response:
  Complete.
- Ensure the partial transaction is rolled back before returning the conflict
  response: Complete.
- Preserve unrelated behavior and avoid broad refactors: Complete.
- Validate with the narrowest relevant tests: Complete.

## Evidence

- Changed `src/awf/api/routes/workspaces.py` to roll back the request session
  before returning the external-id conflict response.
- Added
  `tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_external_id_scope_conflict_rolls_back_rejected_workspace`.
  It failed before the fix because both `"api slice"` and `"docs slice"` rows
  were persisted, then passed after the route rollback.
- Updated
  `tests/unit/api/test_route_error_edges.py::test_workspace_v2_create_reports_task_external_id_conflict`
  to assert the route awaits rollback while preserving the response body.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_external_id_scope_conflict_rolls_back_rejected_workspace -q`
  - Pre-fix result: failed with persisted `["api slice", "docs slice"]`.
  - Post-fix result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q -k external_id`
  - Result: passed, 3 passed and 149 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_route_error_edges.py::test_workspace_v2_create_reports_task_external_id_conflict -q`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py`
  - Result: passed.

## Gaps

None.
