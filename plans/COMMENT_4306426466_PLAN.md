# Comment 4306426466 Plan

## Problem Statement And Scope

Review comment 4306426466 reports that `POST /v1/workspaces` handles
`TaskExternalIdConflictError` by returning a normal `JSONResponse` after
`create_workspace_row()` has already flushed a new `Workspace`. Because the
request dependency commits successful route returns, the rejected workspace can
be committed even though the API returns `409 TASK_EXTERNAL_ID_CONFLICT`.

Scope is limited to the workspace create REST path and its regression coverage.

## Requirements Checklist

- Add a regression test that fails if an external-id conflict persists the
  rejected workspace row.
- Keep the existing structured `409 TASK_EXTERNAL_ID_CONFLICT` response.
- Ensure the partial transaction is rolled back before returning the conflict
  response.
- Preserve unrelated behavior and avoid broad refactors.
- Validate with the narrowest relevant tests.

## Implementation Steps

1. Add an API regression test that creates a workspace, submits a second
   workspace with the same external ID but a different task scope, asserts the
   second response is 409, and verifies only the accepted workspace exists in
   the database for that repo/external ID.
2. Run the new regression test before the fix to confirm it fails.
3. Update the `TaskExternalIdConflictError` handler in
   `src/awf/api/routes/workspaces.py` to roll back the request session before
   returning the structured 409 response.
4. Re-run the focused regression and nearby workspace API tests.
5. Record validation results in `plans/COMMENT_4306426466_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q -k external_id`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_route_error_edges.py::test_workspace_v2_create_reports_task_external_id_conflict -q`
  passes.
