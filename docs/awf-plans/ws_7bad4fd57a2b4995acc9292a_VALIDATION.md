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
  - `240 passed in 240.79s (0:04:00)`
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspaceV2::test_create_workspace_v2_external_id_scope_conflict_returns_structured_error -q`
  - `1 passed in 7.92s`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf`
  - `Success: no issues found in 155 source files`
- Targeted static searches for the raw interval pattern and old
  `Task external_id` wording returned no matches in `src/awf`/`tests/unit`.

## Gaps

None.
