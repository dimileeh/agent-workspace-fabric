# Security Cleanup Audit Validation

Plan reference: `plans/SECURITY_CLEANUP_AUDIT_PLAN.md`

AWF contract reference:
`docs/awf-plans/ws_7bad4fd57a2b4995acc9292a.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add failing regression tests first for still-valid findings. | Complete | Initial regression slice failed on raw PostgreSQL interval SQL and old external-ID conflict wording before source changes. Doctor redaction proof already passed; support-bundle proof passed after fixing the test helper setup. |
| Remove fragile PostgreSQL interval string interpolation while preserving scheduler scoring behavior. | Complete | `src/awf/db/repositories.py` now uses a SQLAlchemy `make_interval` expression through `_postgresql_interval_seconds_expr`; scheduler SQL tests assert `make_interval(...)`, no raw interval literal, and no `EXTRACT(epoch...)` regression. |
| Prevent the fragile interval pattern from returning. | Complete | `tests/unit/db/test_workspace_repository.py` adds a static source assertion against `_postgresql_scheduler_age_boost_expr`; targeted `rg` for the raw pattern in the repository/test path returns no matches. |
| Keep selected create/idempotency/conflict 409 payloads stable and actionable without leaking `task_external_id`. | Complete | REST v1/v2 idempotency and v2 task external-ID conflict tests assert stable error codes, public `detail={"external_id": ...}`, natural-language guidance, and no serialized `task_external_id`. The duplicate MCP workspace-create conflict helper now uses the same public wording and assertion. |
| Prove doctor/support-bundle known-secret sets are redaction-only. | Complete | Doctor and support-bundle tests now include configured API-token and database-password sentinels in log-like/status/doctor/failure payloads; pretty output, serialized bundle/report JSON, and written bundle artifact all exclude the sentinels and include `<redacted>`. |
| Keep scope narrow and avoid active P1 auth/callback/rate-limit/config workstreams. | Complete | Source changes are limited to scheduler SQL expression construction, selected conflict message wording, and the duplicated MCP conflict payload. No auth, callback, rate-limit, production config, executor, scheduler orchestration, generated artifact, workflow, or lockfile changes were made. |

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_cursor_age_boost_uses_timestamp_thresholds tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_age_boost_does_not_use_raw_interval_text tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2MonitorPolicy::test_idempotency_conflicts_when_monitor_policy_changes tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_rejects_external_id_reuse_for_different_scope tests/unit/api/test_workspaces.py::TestIdempotency::test_same_key_different_body_returns_409 tests/unit/api/test_route_error_edges.py::test_workspace_v2_create_reports_task_external_id_conflict tests/unit/service/test_doctor.py::test_doctor_output_redacts_secrets_from_pretty_and_json tests/unit/service/test_support_bundle.py::test_support_bundle_redacts_secrets -q
```

Initial result before source changes: failed as expected on raw interval SQL and
old external-ID conflict wording; doctor proof passed.

Final result after source changes:

```text
8 passed in 10.01s
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py -q
```

Final result:

```text
240 passed in 240.79s (0:04:00)
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py::TestCreateWorkspaceV2::test_create_workspace_v2_external_id_scope_conflict_returns_structured_error -q
```

Result:

```text
1 passed in 7.92s
```

```bash
rg 'text\(f"INTERVAL|INTERVAL '\''[0-9]+ seconds'\''' src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py
```

Result: no matches.

```bash
rg -n 'Task external_id|task_external_id.*already associated|text\(f"INTERVAL|INTERVAL '\''[0-9]+ seconds'\''' src/awf tests/unit
```

Result: no matches.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result:

```text
All checks passed!
```

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result:

```text
Success: no issues found in 155 source files
```

## Files Changed

- `src/awf/db/repositories.py`
- `src/awf/api/routes/workspaces.py`
- `src/awf/mcp/server.py`
- `tests/unit/db/test_workspace_repository.py`
- `tests/unit/api/test_workspaces.py`
- `tests/unit/api/test_route_error_edges.py`
- `tests/unit/service/test_doctor.py`
- `tests/unit/service/test_support_bundle.py`
- `tests/unit/mcp/test_mcp_server.py`
- `plans/SECURITY_CLEANUP_AUDIT_PLAN.md`
- `plans/SECURITY_CLEANUP_AUDIT_VALIDATION.md`

## Gaps

None.
