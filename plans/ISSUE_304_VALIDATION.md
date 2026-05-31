# ISSUE-304 Validation: Host-Port Collision Detection

## Summary

Cross-workspace host-port collision detection at dispatch time is fully
implemented. When a companion's `host_port` is already mapped by a
non-terminal workspace, the API returns HTTP 409 with error code
`HOST_PORT_CONFLICT` **before** any build or provision starts.

## Implementation Checklist

| Item | Status | Notes |
|------|--------|-------|
| `HostPortConflict` dataclass in `base.py` | Done | `@dataclass(frozen=True)` with `host_port: int` and `workspace_id: str` |
| `WorkspaceCreateHostPortConflictError` in `workspaces.py` | Done | `error_code = "HOST_PORT_CONFLICT"`, `detail` dict with both fields |
| `find_host_port_conflicts()` repo method | Done | Python-side JSON parsing (dialect-safe); filters by `ACTIVE_OWNED_PATH_OVERLAP_STATUSES` |
| Conflict check in `WorkspaceService.create()` | Done | Extracts host ports from `req.companions`, raises on first conflict |
| HTTP 409 route handler in `routes/workspaces.py` | Done | Catches `WorkspaceCreateHostPortConflictError`, returns `ErrorResponse` |
| Exports from `__init__.py` | Done | Both `HostPortConflict` and `WorkspaceCreateHostPortConflictError` exported |
| OpenAPI drift check | Done | `generate_openapi.py --check` passes |

## Test Results

```text
11 passed in 9.61s
```

All 11 test cases in
`tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`:

1. `test_collision_returns_conflict` — finds conflict with running workspace
2. `test_no_collision_succeeds` — no conflict when port is free
3. `test_terminal_workspace_not_blocking` — completed workspace ignored
4. `test_multiple_ports_one_companion` — finds specific port from multi-port companion
5. `test_multiple_companions` — finds conflict across companions
6. `test_idempotent_replay_no_collision` — `excluding_workspace_id` works
7. `test_all_terminal_statuses_excluded` — all 5 terminal statuses ignored
8. `test_multiple_conflicting_ports` — returns multiple conflicts
9. `test_no_companions_in_existing_workspace` — empty task_policy safe
10. `test_companion_without_ports` — companion with no ports safe
11. `test_empty_host_ports_query` — empty query returns early

## Broader Test Suite

- `tests/unit/db/` — 265 passed
- ruff — all changed files pass
- mypy — all changed files pass (pre-existing `WorkspaceLogStreamRepository` issue excluded)

## Design Decisions

1. **Python-side JSON parsing** rather than dialect-specific SQL: The
   `task_policy.companions` JSON structure (array of objects, each with a
   `ports` array of `[container, host]` pairs) is awkward to query in raw
   SQL, especially across Postgres JSONB and SQLite `json_extract`. Parsing
   in Python is cleaner, more maintainable, and the query volume is small
   (only non-terminal workspaces' `task_policy` columns).

2. **TOCTOU accepted**: The SELECT-and-then-INSERT race window is documented
   in the error class docstring. Docker Compose itself would reject a port
   conflict at runtime, so this is best-effort early detection.

3. **First conflict only in error**: The service raises on `conflicts[0]`
   for simplicity. The full list is available for future enhancement.

4. **`HostPortConflict` in repo/base.py** rather than service layer: Keeps
   the data class next to `OwnedPathConflict` and other repo-level types,
   avoiding circular imports.

## Files Changed

- `src/awf/db/repositories/base.py` — Added `HostPortConflict` dataclass
- `src/awf/db/repositories/__init__.py` — Exported `HostPortConflict`
- `src/awf/db/repositories/workspace_repo.py` — Added `find_host_port_conflicts()` method
- `src/awf/service/workspaces.py` — Added `WorkspaceCreateHostPortConflictError`, conflict check in `create()`, imported `HostPortConflict` from repo
- `src/awf/api/routes/workspaces.py` — Import and catch `WorkspaceCreateHostPortConflictError` → 409
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py` — 11 test cases
