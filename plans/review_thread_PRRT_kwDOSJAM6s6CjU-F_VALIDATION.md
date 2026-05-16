# Review Thread PRRT_kwDOSJAM6s6CjU-F Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CjU-F_PLAN.md`

## Requirement Status

- Reject blank or missing `Idempotency-Key` values with the existing 400
  response: Complete.
  - Existing coverage in `test_sensitive_controls_require_idempotency_key`
    remains passing.
- Reject trimmed control idempotency keys longer than 128 characters before
  any service/database operation: Complete.
  - `src/awf/api/routes/controls.py` now bounds the trimmed key before calling
    `WorkspaceControlService`.
  - `test_sensitive_controls_reject_idempotency_key_over_database_limit` covers
    cancel, stop, destroy, remonitor, refresh, validate, and rebase.
- Keep exact 128-character keys accepted: Complete.
  - `test_recovery_operation_accepts_idempotency_key_at_database_limit` persists
    an operation with a 128-character key.
- Align the console workspace-control BFF limit with the AWF database column
  and backend control API: Complete.
  - `apps/console/lib/workspace-control-routes.ts` now limits supplied
    idempotency keys to 128 characters.
- Add focused regression tests for backend and console routes: Complete.
  - Backend API and console BFF regressions were added.

## Evidence

Changed files:

- `src/awf/api/routes/controls.py`
- `tests/unit/api/test_workspace_controls_idempotency.py`
- `apps/console/lib/workspace-control-routes.ts`
- `apps/console/lib/workspace-control-routes.test.mjs`

TDD failure evidence before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_reject_idempotency_key_over_database_limit tests/unit/api/test_workspace_controls_idempotency.py::test_recovery_operation_accepts_idempotency_key_at_database_limit -q`
  failed with seven 404 responses instead of the planned 400 response.
- `node --test apps/console/lib/workspace-control-routes.test.mjs` failed
  because the BFF proxied a 129-character key.

Passing validation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_sensitive_controls_reject_idempotency_key_over_database_limit tests/unit/api/test_workspace_controls_idempotency.py::test_recovery_operation_accepts_idempotency_key_at_database_limit -q`
  - 8 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py -q`
  - 69 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/controls.py tests/unit/api/test_workspace_controls_idempotency.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/controls.py`
  - Passed.
- `node --test apps/console/lib/workspace-control-routes.test.mjs`
  - 6 passed.
- `npm --prefix apps/console run lint`
  - Passed.
- `npm --prefix apps/console run typecheck`
  - Passed.

## Gaps

None.
