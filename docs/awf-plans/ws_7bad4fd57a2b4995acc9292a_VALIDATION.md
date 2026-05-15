# AWF Security Cleanup Audit Validation

Plan reference: `docs/awf-plans/ws_7bad4fd57a2b4995acc9292a.md`

Protocol validation artifact:
`plans/SECURITY_CLEANUP_AUDIT_VALIDATION.md`

## Summary

All requirements from the workspace plan are complete.

- PostgreSQL scheduler scoring no longer uses raw interval string
  interpolation; it uses a SQLAlchemy `make_interval` expression and has SQL
  plus static regression coverage.
- Selected REST and MCP create/idempotency/conflict error payloads retain stable
  error codes and actionable public `external_id` guidance without serialized
  `task_external_id` leakage.
- Doctor/support-bundle tests prove configured API tokens and database
  passwords are used for redaction and are not emitted in pretty output,
  serialized reports, collected bundles, or written bundle JSON.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py -q`
  - Initial validation: `240 passed in 240.79s (0:04:00)`
  - Iteration 1 revalidation: `240 passed in 212.69s (0:03:32)`
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspaceV2::test_create_workspace_v2_external_id_scope_conflict_returns_structured_error -q`
  - `1 passed in 7.92s`
- Iteration 1: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_direct_v1_create_replays_same_payload_and_rejects_conflict tests/unit/api/test_workspaces.py::TestIdempotency::test_same_key_different_body_returns_409 -q`
  - `2 passed in 3.12s`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf`
  - `Success: no issues found in 155 source files`
- Targeted static searches for the raw interval pattern and old
  `Task external_id` wording returned no matches in `src/awf`/`tests/unit`.
- Iteration 1 targeted `task_external_id` search found only route/model wiring
  and negative test assertions in the scoped v1/v2 API files.

## Iteration 1

The AWF conformance check reported that v1 workspace create idempotency conflict
tests only asserted `status_code`/`error_code`. The current implementation did
not leak internal fields, so the fix was a focused proof gap: both the direct
v1 route test and the HTTP v1 idempotency conflict test now assert the
serialized 409 `IDEMPOTENCY_CONFLICT` payload excludes `task_external_id` and
related internal column-style names (`task_kind`, `idempotency_key`, and hash
fields).

## Gaps

None.
