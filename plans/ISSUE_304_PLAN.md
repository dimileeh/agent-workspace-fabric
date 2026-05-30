# ISSUE-304: Host-Port Collision Detection at Dispatch Time

## Problem

When two workspace companions request the same `host_port` on the same host
node, Docker Compose will fail at provisioning time with a non-descriptive
error. The user gets an `INFRASTRUCTURE_FAILURE` status and has to dig through
logs to discover the port conflict. The collision should be caught **before**
any build/provision step, returning HTTP 409 with the conflicting port and
workspace ID.

## Scope

**In scope** (this PR):
- Dispatch-time validation: query non-terminal workspaces for host-port
  conflicts, raise a domain error that maps to HTTP 409.
- New error: `WorkspaceCreateHostPortConflictError` with `host_port` and
  `conflicting_workspace_id` fields.
- Dialect-aware SQL: Postgres uses JSONB operators, SQLite uses
  `json_extract`. Both must work.
- Terminal statuses (`completed`, `failed`, `cancelled`, `destroying`,
  `destroyed`) MUST NOT trigger false collisions.
- Idempotent replays of the same request MUST NOT 409.

**Out of scope** (future work):
- Auto-allocating ports.
- Surfacing host-port conflicts in `INFRASTRUCTURE_FAILURE` events.
- Per-host/node scoping (single-node assumption for now; scope by `node_id`
  when multi-node lands).

## Architecture

### 1. Error class

```python
# src/awf/service/workspaces.py

class WorkspaceCreateHostPortConflictError(Exception):
    """Raised when a companion's host_port is already mapped by a non-terminal workspace."""

    error_code = "HOST_PORT_CONFLICT"

    def __init__(
        self,
        *,
        host_port: int,
        conflicting_workspace_id: str,
    ) -> None:
        self.host_port = host_port
        self.conflicting_workspace_id = conflicting_workspace_id
        self.detail = {
            "host_port": host_port,
            "conflicting_workspace_id": conflicting_workspace_id,
        }
        super().__init__(
            f"Host port {host_port} is already in use by workspace "
            f"{conflicting_workspace_id}"
        )
```

### 2. Repository method: `find_host_port_conflicts`

Add to `WorkspaceRepository` in
`src/awf/db/repositories/workspace_repo.py`.

Companions live inside `task_policy.companions[]` as JSON. Each companion
has a `ports` field of shape `[[container_port, host_port], ...]`.

The method:
1. Extracts all host_ports from the new request's task_policy companions.
2. Queries the database for non-terminal workspaces on the same repo_url/
   branch_base, extracts their companion host_ports from `task_policy`.
3. Returns a list of `(host_port, conflicting_workspace_id)` pairs for
   collisions.

**Dialect-aware JSON SQL:**
- Postgres: `task_policy->'companions'` with `jsonb_array_elements`
- SQLite: `json_extract(task_policy, '$.companions')` with recursive extraction

**TOCTOU note:** There is a time-of-check/time-of-use window between the
SELECT and INSERT. This is acceptable because Docker Compose itself would
fail in the same scenario — the database check is a best-effort early
detection, not a serialisation guarantee. A comment in the method
documents this.

**Terminal status exclusion:** Only statuses in
`ACTIVE_OWNED_PATH_OVERLAP_STATUSES` (requested, provisioning, ready,
running, validating, pushing, monitoring_pr) trigger collisions.

### 3. Service-layer call

In `WorkspaceService.create()` (or its delegate `create_workspace_row`),
after idempotency and disk checks, call
`repo.find_host_port_conflicts(...)` and raise
`WorkspaceCreateHostPortConflictError` if any are found.

### 4. Route-level mapping

In `src/awf/api/routes/workspaces.py`, the `create_workspace` endpoint
already catches `WorkspaceCreateIdempotencyConflictError` (→ 409) and
`WorkspaceCreateInsufficientDiskError` (→ 409/503). Add a catch for
`WorkspaceCreateHostPortConflictError` → 409 with `ErrorResponse`:

```python
except WorkspaceCreateHostPortConflictError as exc:
    await session.rollback()
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(
            error_code=exc.error_code,
            message=exc.message,
            detail=exc.detail,
        ).model_dump(),
    )
```

## Test Plan

### Unit tests (`tests/unit/`)

1. **collision_returns_409**: Request a workspace with companion port 8080
   when another non-terminal workspace already claims 8080 → HTTP 409 with
   `HOST_PORT_CONFLICT` error code, `host_port=8080`,
   `conflicting_workspace_id=<existing>`.

2. **no_collision_succeeds**: No port overlap → workspace created normally.

3. **terminal_workspace_not_blocking**: Same port 8080 but existing
   workspace is `completed` → workspace created normally (no 409).

4. **multiple_ports_one_companion**: A companion with `[[80, 8080],
   [443, 8443]]` where 8080 is already taken → 409 with
   `host_port=8080`.

5. **multiple_companions**: Two companions where one conflicts → 409.

6. **idempotent_replay_no_collision**: Replay the same idempotency key
   (same payload) → 202 (replay), not 409. The port is "self-owned."

7. **dialect_aware_query_sqlite**: Verify the SQLite JSON extraction path
   works correctly (the test will use the SQLite session fixture).

## Files to modify

| File | Change |
|------|--------|
| `src/awf/service/workspaces.py` | Add `WorkspaceCreateHostPortConflictError`, export in `__all__` |
| `src/awf/db/repositories/workspace_repo.py` | Add `find_host_port_conflicts()` |
| `src/awf/api/routes/workspaces.py` | Catch new error → 409 |
| `src/awf/db/repositories/base.py` | Add `HOST_PORT_CONFLICT_STATUSES` constant (reuse `ACTIVE_OWNED_PATH_OVERLAP_STATUSES`) |
| `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_host_port.py` | New test file |
| `tests/unit/service/test_workspaces.py` | Add unit tests for service-layer collision detection |
| `tests/unit/api/test_workspaces_direct.py` | Add API-level 409 test |

## Rollout

This is a pure dispatch-time validation — no migration, no schema change,
no backward-compatibility concern. If the conflict detection has a bug,
the worst case is a false positive (409 when there is no real conflict),
which the user can retry. False negatives are caught by Docker Compose at
provisioning time, so we degrade gracefully.

## Open questions

1. Should we scope by `node_id` now? — No, single-node assumption for
   now. The query is structured to accept a `node_id` filter later.
2. Should we auto-allocate ports? — Out of scope; this PR only detects
   collisions.
