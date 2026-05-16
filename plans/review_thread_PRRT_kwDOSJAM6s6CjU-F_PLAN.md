# Review Thread PRRT_kwDOSJAM6s6CjU-F Plan

## Problem Statement and Scope

The operator control endpoints accept an `Idempotency-Key` longer than the
`operations.idempotency_key` database column. PostgreSQL rejects the overlong
value during operation creation, so the endpoint can return a server error
instead of a deterministic client error. The console operator-control BFF also
permits keys longer than the same database contract.

Scope is limited to the workspace control idempotency-key validation paths and
their focused regressions.

## Requirements Checklist

- [ ] Reject blank or missing `Idempotency-Key` values with the existing 400
  response.
- [ ] Reject trimmed control idempotency keys longer than 128 characters before
  any service/database operation.
- [ ] Keep exact 128-character keys accepted.
- [ ] Align the console workspace-control BFF limit with the AWF database
  column and backend control API.
- [ ] Add focused regression tests for backend and console routes.

## Implementation Steps

1. Add failing backend API regressions for overlong and exact-limit control
   idempotency keys.
2. Add a failing console BFF regression for 129-character keys.
3. Add a shared backend constant and length check in
   `src/awf/api/routes/controls.py`.
4. Update the console BFF maximum from 200 to 128 and adjust the error message.
5. Run the focused Python and console tests, then lint/typecheck the touched
   surfaces as practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::<focused-tests> -q`
  must pass.
- `node --test apps/console/lib/workspace-control-routes.test.mjs` must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/controls.py tests/unit/api/test_workspace_controls_idempotency.py`
  must pass.
- `npm --prefix apps/console run lint` or a narrower available console check
  must pass if dependencies are available.
