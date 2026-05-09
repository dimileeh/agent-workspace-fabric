# Plan: P1 MCP Parity — Read-Only Operator Surfaces for Tasks, Locks, and Service Health

## Scope

Add first-class MCP tools for the high-value AWF operator read surfaces that exist in
REST but have no MCP counterpart. This is a **read-only parity slice**: we expose the
same data the REST API already serves, through the same shared service functions,
so MCP and REST stay in lockstep without coupling to route handlers.

**Tools to add (7 total):**

| MCP Tool Name | REST Endpoint | Service Function | Response Schema |
|---|---|---|---|
| `awf_list_tasks` | `GET /v1/tasks` | `TaskAttemptRepository.list_latest` + `WorkspaceRepository.list_without_task_attempts` | `TaskListResponse` |
| `awf_list_task_attempts` | `GET /v1/tasks/{task_ref}/attempts` | `TaskRepository.get_by_ref` + `TaskAttemptRepository.list_for_task` | `TaskAttemptListResponse` |
| `awf_list_locks` | `GET /v1/locks` | `list_workspace_lock_page_for_session` from `awf.service.locks` | `WorkspaceLockListResponse` |
| `awf_get_service_readiness` | `GET /readyz` | Inline readiness checks (DB, Docker, agent runtime, orphan resources) | `ReadyResponse` |
| `awf_get_service_health` | `GET /healthz` | Inline liveness | `HealthResponse` |
| `awf_remonitor_workspace` | `POST /v1/workspaces/{id}/remonitor` | `WorkspaceControlService.remonitor_workspace` | `WorkspaceControlResponse` |
| `awf_request_workspace_validation` | `POST /v1/workspaces/{id}/validate` | `WorkspaceControlService.request_validate_workspace` | `OperationResponse` |

### Rationale for inclusion

- **Tasks + attempts**: The task is a first-class operator concept (grouping retry
  chains via `TaskAttempt`). Workspace-overview is workspace-centric; task listing
  is the canonical way to see canonical-attempt, candidate readiness, and
  redispatch/supersession chains. High-value per the PRD merge-safety model.

- **Locks**: The paginated lock list shows which workspace owns which paths, with
  per-row overlap-risk summaries. The overlap *graph* already exists in MCP, but
  the lock list is a distinct tabular operator surface.

- **Service readiness/health**: `/readyz` is critical for operators to diagnose AWF
  substrate health (DB, Docker daemon, compose plugin, agent runtime image, orphan
  resources, agent readiness). `/healthz` is a lightweight liveness check. Both are
  standard SRE surfaces.

- **Remonitor + validate controls**: These are operator recovery actions (re-trigger
  PR monitor, re-trigger validation). They are already in `WorkspaceControlService`
  and follow the same error-handling pattern as the existing cancel/stop/destroy MCP
  tools. They are scoped to AWF-managed operations (not arbitrary shell or Docker
  exec).

### Explicit non-goals

- **Refresh/rebase controls** (`request_refresh_workspace`, `request_rebase_workspace`):
  deferred to a follow-on slice to keep this one narrow. They follow the same pattern
  and can be added trivially once remonitor/validate are proven.
- **Retry endpoint** (`POST /v1/workspaces/{id}/retry`): uses a different service
  (`retry_workspace_row`) and response schema; deferred.
- **Callback subscription** (`GET/POST /v1/callbacks`): moderate value, write surface,
  deferred.
- **Secret leases** (`GET /v1/workspaces/{id}/secret-leases`): intentionally excluded
  from MCP by design — secret data should not flow through MCP tool responses.
- **Global events** (`GET /v1/events`): marginal value vs. per-workspace event listing
  already in MCP.
- **Artifact download** (`GET /v1/workspaces/{id}/artifacts/download`): binary file;
  MCP already lists metadata. Binary transport doesn't fit MCP tool model.
- **WebSocket** (`WS /v1/workspaces/{id}/ws`): streaming is incompatible with MCP
  request-response model.
- **Any exposure of arbitrary shell or unrestricted Docker exec**: explicitly out
  of scope per task requirements.

---

## Intended Files/Modules to Touch

### Production changes

| File | Intended changes |
|---|---|
| `src/awf/mcp/server.py` | Add 7 new tool registrations in `build_mcp_server()`. Each uses shared service functions (not route handlers). Read-only tools return `StructuredToolResult`; control tools handle `WorkspaceControlError` via `_tool_error`. Import new schemas and service functions. |

### Test changes (TDD: write failing tests first)

| File | Intended changes |
|---|---|
| `tests/unit/mcp/test_mcp_operator_surfaces.py` | Add contract tests for all 7 new tools: registration, payload parity with REST, reason codes, missing-resource null/error handling, control tool error mapping. Extend `NEW_OPERATOR_TOOLS` set. |
| `tests/unit/mcp/test_mcp_server.py` | Add tool-registration tests for `awf_list_tasks`, `awf_list_task_attempts`, `awf_list_locks`, `awf_get_service_readiness`, `awf_get_service_health`, `awf_remonitor_workspace`, `awf_request_workspace_validation`. Add input-schema contract tests for argument constraints. |

### Files NOT to touch

- `src/awf/api/routes/*` — no route handler changes; MCP uses shared services.
- `src/awf/api/schemas.py` — no schema changes needed; tools reuse existing schemas.
- `src/awf/service/locks.py`, `src/awf/service/controls.py`, etc. — no service changes needed.
- Migrations, lockfiles, config, console, README, unrelated docs.

---

## Tests to Write First (TDD)

### Phase 1: Failing registration + contract tests

Write these as failing tests before any production code change. All tests in
`tests/unit/mcp/`.

1. **`test_operator_parity_tools_registered`** (extend existing in `test_mcp_operator_surfaces.py`)
   - Add the 5 new read-only tool names to `NEW_OPERATOR_TOOLS`:
     `awf_list_tasks`, `awf_list_task_attempts`, `awf_list_locks`,
     `awf_get_service_readiness`, `awf_get_service_health`.
   - Assert each is registered and has "read-only" and "operator" in description,
     no "shell" or "docker exec".

2. **`test_control_tools_are_described_as_operator_controls`** (extend in `test_mcp_server.py`)
   - Add `awf_remonitor_workspace`, `awf_request_workspace_validation` to the
     control-tool description assertions: assert "operator control" and
     "not shell access" in description.

3. **`test_operator_parity_tool_argument_contracts`** (extend in `test_mcp_server.py`)
   - Assert input schema constraints for new tools:
     - `awf_list_tasks`: `limit` default 50, range [1,500]; optional `status`, `agent`, `repo_url` with string length constraints.
     - `awf_list_task_attempts`: `task_ref` required string; `limit` default 100, range [1,500].
     - `awf_list_locks`: `limit` default 50, range [1,500]; optional `cursor` max_length 256.
     - `awf_get_service_readiness`: no required args.
     - `awf_get_service_health`: no required args.
     - `awf_remonitor_workspace`: `workspace_id` required; optional `reason`.
     - `awf_request_workspace_validation`: `workspace_id` required; optional `reason`, `requested_tier`.

### Phase 2: Failing payload-parity tests

These compare MCP tool output to REST response for identical seeded data,
proving payloads align with the corresponding REST schema fields.

4. **`test_task_listing_tool_matches_rest_payload`**
   - Seed workspaces with task attempts, canonical merge candidates.
   - Call `GET /v1/tasks` via REST and `awf_list_tasks` via MCP.
   - Assert identical payloads. Verify fields like `attempt_id`,
     `is_canonical_for_merge`, `canonical_attempt_id`, `readiness`,
     `agent_model`, `agent_model_source`, `llm_usage`.

5. **`test_task_attempts_tool_matches_rest_payload`**
   - Seed a task with multiple attempts (parent/redispatch chain).
   - Call `GET /v1/tasks/{ref}/attempts` via REST and `awf_list_task_attempts`
     via MCP.
   - Assert identical payloads. Verify `attempt_number`, `parent_attempt_id`,
     `superseded_by_attempt_id`, `is_canonical_for_merge`, `candidate_status`.

6. **`test_locks_tool_matches_rest_payload`**
   - Seed overlapping active workspaces with owned paths.
   - Call `GET /v1/locks` via REST and `awf_list_locks` via MCP.
   - Assert identical payloads. Verify `owned_paths`, `overlap_risks`,
     pagination (`next_cursor`, `has_more`).

7. **`test_locks_invalid_cursor_returns_structured_mcp_error`**
   - Call `awf_list_locks` with `cursor="not-a-cursor"`.
   - Assert `isError=True`, `structuredContent` has `error_code="INVALID_CURSOR"`
     and message matching REST error detail.

8. **`test_service_readiness_tool_matches_rest_payload`**
   - Wire fake command runner and disk check to the operator stack.
   - Call `GET /readyz` via REST and `awf_get_service_readiness` via MCP.
   - Assert same top-level structure: `service`, `version`, `status`, `checks`,
     `agent_readiness`. Verify each check has `ok`, `status`, `reason`.

9. **`test_service_health_tool_returns_healthz_payload`**
   - Call `GET /healthz` via REST and `awf_get_service_health` via MCP.
   - Assert identical payloads: `status="ok"`, `service="awf"`, version set.

10. **`test_missing_task_attempts_return_structured_error`**
    - Call `awf_list_task_attempts` with `task_ref="nonexistent"`.
    - Assert `isError=True`, `error_code="NOT_FOUND"`.

11. **`test_remonitor_workspace_tool_matches_rest_response`**
    - Seed a workspace in `monitoring_pr` with a PR URL.
    - Call the remonitor MCP tool; assert it returns `WorkspaceControlResponse`
      payload with correct operation_id and status.
    - Verify error mapping: call on wrong-state workspace returns
      `WorkspaceControlError` as structured MCP error.

12. **`test_request_workspace_validation_tool_matches_rest_response`**
    - Seed a workspace with PR URL ready for validation.
    - Call the validate MCP tool; assert it returns `OperationResponse` payload.
    - Verify error mapping for wrong-state workspace.

13. **`test_read_only_operator_tools_use_shared_services_not_route_handlers`**
    - Extend the existing monkeypatch test to also monkeypatch task route
      handlers, lock route handlers, health route handlers. Verify MCP calls
      still succeed (proving no coupling to route handlers).

---

## Implementation Steps

### Step 1: Add tool registrations in `src/awf/mcp/server.py`

For each new tool, add a closure inside `build_mcp_server()` following the
existing patterns:

- **Read-only tools**: Use `StructuredToolResult`, call shared service/session-layer
  functions, wrap in the correct Pydantic response model, return via `_tool_result()`.
- **Read-only with workspace_id-scoped null**: Use `CallToolResult` + `_null_tool_result()`
  when the workspace doesn't exist (matching existing `awf_list_workspace_validation`
  pattern).
- **Control tools**: Use `try/except WorkspaceControlError` + `_tool_error()`, matching
  existing `awf_cancel_workspace` / `awf_stop_workspace` pattern.

Key implementation details:

1. `awf_list_tasks`: Call `TaskAttemptRepository(session).list_latest(...)` and
   `WorkspaceRepository(session).list_without_task_attempts(...)`, then build
   `TaskResponse` objects using the same `_task_from_attempt` and `_task_from_workspace`
   logic from the REST route (factored out or inlined).

2. `awf_list_task_attempts`: Call `TaskRepository(session).get_by_ref(task_ref)`;
   if None, return structured error. Then `TaskAttemptRepository(session).list_for_task(...)`
   and build `TaskAttemptResponse` items.

3. `awf_list_locks`: Call `list_workspace_lock_page_for_session(session, ...)` from
   `awf.service.locks`. Catch `InvalidWorkspaceLockCursorError` → structured
   `INVALID_CURSOR` error. Build `WorkspaceLockListResponse`.

4. `awf_get_service_readiness`: Wire readiness-check logic. Requires `build_mcp_server`
   to accept optional `command_runner` and readiness-related providers (matching
   existing pattern of `disk_check_provider`, `orphan_resource_summary_provider`).
   For simplicity, delegate to the health-check functions from
   `awf.api.routes.health` but call the underlying check functions
   (`_check_db`, `_check_docker_cli`, etc.) or the `readyz` route logic
   via a provider pattern. If the full readiness requires Docker access and
   that isn't available in test, use a provider injection approach matching
   the existing `disk_check_provider` pattern.

5. `awf_get_service_health`: Return `HealthResponse` payload. No dependencies.

6. `awf_remonitor_workspace`: Accept `workspace_id`, optional `reason`. Instantiate
   `WorkspaceControlService` with session, call `remonitor_workspace(...)`.
   Handle `WorkspaceControlError` via `_tool_error()`.

7. `awf_request_workspace_validation`: Accept `workspace_id`, optional `reason`,
   optional `requested_tier`. Instantiate `WorkspaceControlService`, call
   `request_validate_workspace(...)`. Handle `WorkspaceControlError` via `_tool_error()`.

### Step 2: Wire control tools through `WorkspaceService`

The existing cancel/stop/destroy tools go through `service.cancel_workspace(...)`,
`service.stop_workspace(...)`, `service.destroy_workspace(...)`. The new control
tools (`remonitor`, `validate`) need equivalent `WorkspaceService` methods that
delegate to `WorkspaceControlService`. Add thin delegation methods to
`WorkspaceService` (or call `WorkspaceControlService` directly from the MCP closures
with the session, matching the pattern in the REST route).

**Preferred approach**: Call `WorkspaceControlService` directly from MCP tool closures
using `service.session_factory()` — this avoids modifying `WorkspaceService` and
keeps MCP closures consistent with REST route structure. The cancel/stop/destroy
tools already go through `service.*` because those methods pre-exist on
`WorkspaceService`; adding new `WorkspaceService` methods for every control operation
creates unnecessary surface. The MCP closures can directly construct
`WorkspaceControlService(session, ...)` the way the REST routes do.

---

## Validation Commands

```bash
# Lint and typecheck
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/mcp
uv run --python 3.12 --extra dev mypy src/awf

# Unit tests for MCP only (primary validation)
uv run --python 3.12 --extra dev pytest tests/unit/mcp -q

# Broader regression check
uv run --python 3.12 --extra dev pytest tests/unit -q

# Coverage (if touching core behavior broadly)
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

---

## Risks, Assumptions, and Explicit Non-goals

### Risks

1. **Readiness tool needs Docker access**: The `/readyz` endpoint runs real Docker
   subcommands (`docker --version`, `docker info`, `docker compose version`).
   In test environments Docker may be unavailable. Mitigation: inject a
   `command_runner` provider into `build_mcp_server` (matching existing
   `disk_check_provider` pattern) and default to a no-op/subprocess runner.
   If no provider is configured, return a degraded readiness payload with
   `status="degraded"` and per-check `reason="PROVIDER_NOT_CONFIGURED"` for
   Docker-dependent checks.

2. **Task attempt route depends on `_task_from_attempt` helper**: This logic
   lives in `src/awf/api/routes/tasks.py` as a module-private function. The MCP
   tool needs the same payload construction. Options:
   - Inline the logic (duplicative but simple and keeps MCP route-independent).
   - Extract to a shared service function in `awf.service.tasks` (cleaner but
     adds a new file; may overlap with other workspaces).
   **Preferred**: Extract a minimal `build_task_list_response(session, ...)` and
   `build_task_attempt_list_response(session, task_ref, ...)` to
   `awf.service.tasks` so both REST and MCP use the same code. This aligns
   with the "shared services not route handlers" architecture.

3. **Overlap with other workspaces**: The coordination warnings flag potential
   overlap on `src/awf/api/schemas.py`. This plan does NOT touch schemas.py.
   The `src/awf/mcp/server.py` file is owned by this workspace and does not
   appear in the overlap list. The new service module `awf.service.tasks` (if
   created) would be new, not overlapping.

4. **Control tool idempotency**: REST control endpoints require `Idempotency-Key`
   header. MCP tools don't have headers. The MCP tools will generate a default
   idempotency key (e.g. `mcp-{tool_name}-{workspace_id}-{timestamp}`) to preserve
  idempotency semantics. This matches how the existing MCP cancel/stop/destroy
  tools work (they also lack Idempotency-Key but go through `WorkspaceService`
  which generates one).

5. **Coverage target**: The repo targets 99% coverage. New tool registrations are
  thin wrappers; coverage depends on test exercise. The extensive contract and
  parity tests above should keep coverage at or above the target.

### Assumptions

- The shared service functions (`TaskAttemptRepository.list_latest`,
  `list_workspace_lock_page_for_session`, etc.) are stable and their return
  types match the Pydantic response schemas via `model_validate`.
- `WorkspaceControlService` is safe to instantiate directly from MCP closures
  with a session from the session factory.
- The read-only tool descriptions must include "read-only" and "operator" and
  exclude "shell" / "docker exec" per the existing test assertions.
- Control tools must include "operator control" and "not shell access" in
  descriptions per existing assertions.

### Explicit non-goals

- Refresh, rebase, and retry control tools (follow-on slice).
- Callback subscription tools (moderate value, deferred).
- Secret lease tools (intentionally excluded for security).
- Binary artifact download (doesn't fit MCP model).
- WebSocket streaming (incompatible with MCP model).
- Changes to REST routes, schemas, or service layer (except potentially
  extracting task-list logic to a shared service module).
- Any lowering of coverage, `fail_under`, workspace coverage requirements,
  or PRD quality gates.
