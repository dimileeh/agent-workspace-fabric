# Plan: MCP Global Events Parity (P1)

Workspace: `ws_32a3971e4aa147c08ed46683`
Backlog slice: `TODO§P1-mcp-global-events`

## Problem

AWF has `GET /v1/events` (global events) and `GET /v1/workspaces/{workspace_id}/events` (workspace-scoped events) in REST, but MCP only exposes the workspace-scoped tool `awf_list_workspace_events`. There is no `awf_list_events` tool for global events. Additionally, the existing `awf_list_workspace_events` returns a raw `list[dict] | None` instead of a `WorkspaceEventListResponse` envelope, so it is marked `MCP partial` in the parity matrix.

## Goal

1. Add `awf_list_events` MCP tool matching `GET /v1/events` with REST-compatible `WorkspaceEventListResponse` envelope.
2. Upgrade `awf_list_workspace_events` to return the `WorkspaceEventListResponse` envelope instead of a raw list.
3. Update capability metadata so both `global_events` and `workspace_events` change from `MCP partial` to `MCP implemented`.
4. Update parity docs (`MCP_CLIENT_PARITY.md`, `MCP_REFERENCE.md`) to reflect the new tool.
5. Check off `TODO§P1-mcp-global-events` in `TODO/pre-gke-industrial-readiness.md`.

## Intended Files/Modules

### Source changes
1. **`src/awf/mcp/server.py`**
   - Add `awf_list_events` tool (global, no workspace_id required; optional `workspace_id` filter, `event_type` filter, `limit` 1-500 default 50).
   - Migrate `awf_list_workspace_events` to return `StructuredToolResult` with `WorkspaceEventListResponse` envelope (matching the `awf_list_operations` pattern).

2. **`src/awf/service/workspaces.py`**
   - Add a `list_global_events` method (or modify `list_events` to accept optional `workspace_id`).
   - The new method should use `WorkspaceEventRepository.list()` with `workspace_id=None` (which already supports global queries) and return `WorkspaceEventListResponse`.

3. **`tests/unit/contracts/_capabilities.py`**
   - `global_events`: set `mcp_tool="awf_list_events"`, `parity_status="MCP implemented"`, remove `parity_backlog_slice` (set to `None` or empty string per pattern), add `mcp_request_fields` and `mcp_required_fields`.
   - `workspace_events`: set `parity_status="MCP implemented"`, remove `parity_backlog_slice`, add `mcp_request_fields` reflecting the envelope upgrade.

4. **`tests/unit/mcp/test_mcp_server.py`**
   - Add failing tests first for the new `awf_list_events` tool and the `awf_list_workspace_events` envelope upgrade.

5. **`tests/unit/mcp/test_mcp_operator_surfaces.py`**
   - Add `awf_list_events` to `NEW_OPERATOR_TOOLS` and `BOUNDED_READ_ONLY_LIST_TOOLS`.
   - Add tests for global events parity with REST.

6. **`tests/unit/mcp/test_mcp_client_parity_docs.py`**
   - Add global events to `READ_ONLY_OPERATOR_ROWS`.
   - Update workspace events row to reflect the tool now returns envelope.

7. **`docs/MCP_CLIENT_PARITY.md`**
   - Update the "Workspace events" row: add `awf_list_events` tool, change status to `MCP implemented`, remove backlog slice, update Security Boundary to remove "no global event tool" language, update MCP tool name cell to list both `awf_list_events` and `awf_list_workspace_events`.

8. **`docs/MCP_REFERENCE.md`**
   - Add `awf_list_events` to the tool table.
   - Update `awf_list_workspace_events` description to note envelope return.
   - Update stale "global workspace event streaming" backlog language.

9. **`TODO/pre-gke-industrial-readiness.md`**
   - Check off `TODO§P1-mcp-global-events`.

## TDD Sequence (write failing tests first)

### Step 1: Failing tests for `awf_list_events` (global event tool)

**File: `tests/unit/mcp/test_mcp_server.py`**
- `TestGlobalEvents::test_list_events_returns_empty_list` — call `awf_list_events` with no workspaces, expect `{"items": [], "has_more": False, "limit": 50, "cursor": None, "next_cursor": None}`.
- `TestGlobalEvents::test_list_events_returns_events_across_workspaces` — create 2 workspaces with events, call `awf_list_events`, expect events from both, ordered newest-first.
- `TestGlobalEvents::test_list_events_filters_by_workspace_id` — call `awf_list_events(workspace_id=...)`, expect only that workspace's events.
- `TestGlobalEvents::test_list_events_filters_by_event_type` — call `awf_list_events(event_type=...)`, expect filtered results.
- `TestGlobalEvents::test_list_events_respects_limit` — pass `limit=2` when more events exist, expect at most 2 items and `has_more=True`.
- `TestGlobalEvents::test_list_events_limit_bounds` — verify schema limit `ge=1, le=500`.

**File: `tests/unit/mcp/test_mcp_operator_surfaces.py`**
- Add `awf_list_events` to `NEW_OPERATOR_TOOLS`.
- Test that `awf_list_events` is registered and has "read-only" and "operator" in its description.
- Test that `awf_list_events` has `limit` in input schema with `minimum=1, maximum<=500`.
- Test empty-state parity: call REST `GET /v1/events` and MCP `awf_list_events`, assert matching payloads.

**File: `tests/unit/mcp/test_mcp_client_parity_docs.py`**
- Add `"Global events": {"awf_list_events"}` to `READ_ONLY_OPERATOR_ROWS`.
- Add `test_global_events_row_is_implemented()` that finds the "Global events" (or "Workspace events") row and asserts `MCP implemented` status, no `TODO§` backlog slice.

### Step 2: Failing tests for `awf_list_workspace_events` envelope upgrade

**File: `tests/unit/mcp/test_mcp_server.py`**
- Update existing `TestWorkspaceEvents` tests to expect `StructuredToolResult` with `WorkspaceEventListResponse` envelope (`items`, `has_more`, `limit`, `cursor`, `next_cursor`) instead of raw `list[dict] | None`.
- Test that `awf_list_workspace_events` for a missing workspace returns `structuredContent=None` (not raw `None`).
- Test that `has_more` is `False` for results within limit.

### Step 3: Failing tests for capability metadata

**File: `tests/unit/contracts/_capabilities.py`**
- `global_events`: `mcp_tool="awf_list_events"`, `parity_status="MCP implemented"`, no `parity_backlog_slice` starting with `TODO§`.
- `workspace_events`: `parity_status="MCP implemented"`, no `parity_backlog_slice` starting with `TODO§`.

### Step 4: Failing tests for parity matrix crossref

**File: `tests/unit/mcp/test_mcp_parity_matrix_crossref.py`**
- The `"Workspace events"` row should no longer have `MCP partial` status.
- `awf_list_events` should be found in both the parity matrix and in `server.py`.
- No `TODO§P1-mcp-global-events` backlog should remain on implemented rows.

## Implementation Sequence (make tests green)

### Step 5: Add service method for global events

**File: `src/awf/service/workspaces.py`**
- Add `list_global_events` method that accepts `workspace_id: str | None = None`, `event_type: str | None = None`, `limit: int = 50`, returns `WorkspaceEventListResponse`.
- Use the `limit + 1` trick for `has_more` computation (matching `awf_list_operations` pattern).
- The existing `WorkspaceEventRepository.list()` already supports `workspace_id=None` for global queries.

### Step 6: Add `awf_list_events` MCP tool

**File: `src/awf/mcp/server.py`**
- New tool `awf_list_events` with parameters: `workspace_id: str | None = None`, `event_type: str | None = None`, `limit: int = Field(default=50, ge=1, le=500)`.
- Returns `StructuredToolResult` via `_tool_result(response.model_dump(mode="json"))`.
- Description includes "read-only" and "operator" keywords.
- Delegates to `service.list_global_events(...)`.

### Step 7: Upgrade `awf_list_workspace_events` to envelope

**File: `src/awf/mcp/server.py`**
- Change return type from `list[dict[str, Any]] | None` to `StructuredToolResult`.
- Delegate to service method that returns `WorkspaceEventListResponse`.
- For missing workspace, return `_tool_result(None)` with `isError=False` (matching the pattern used by other bounded read tools like `awf_list_workspace_validation`).

### Step 8: Update capability metadata

**File: `tests/unit/contracts/_capabilities.py`**
- Update `global_events` capability: `mcp_tool="awf_list_events"`, `parity_status="MCP implemented"`, `parity_backlog_slice=""` (empty, per pattern of other implemented rows).
- Update `workspace_events` capability: `parity_status="MCP implemented"`, `parity_backlog_slice=""`.
- Add `mcp_request_fields` and `mcp_required_fields` for `global_events`.

### Step 9: Update docs

**File: `docs/MCP_CLIENT_PARITY.md`**
- Update "Workspace events" row:
  - MCP tool name: `` `awf_list_events`, `awf_list_workspace_events` ``
  - Status: `MCP implemented`
  - Backlog Slice: `—`
  - Security Boundary: `require_api_token` (remove the "no global event tool" caveat)

**File: `docs/MCP_REFERENCE.md`**
- Add row: `| awf_list_events | Read-only operator global event listing, supporting optional workspace and event-type filters. |`
- Update `awf_list_workspace_events` description to mention envelope return.

**File: `TODO/pre-gke-industrial-readiness.md`**
- Change `- [ ] TODO§P1-mcp-global-events` to `- [x] TODO§P1-mcp-global-events`.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py tests/unit/mcp/test_mcp_operator_surfaces.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Broader coverage if core behavior is touched:
```bash
uv run --python 3.12 --extra dev pytest tests/unit -q
```

OpenAPI spec drift check:
```bash
python scripts/generate_openapi.py --check
```

## Risks and Assumptions

- **Envelope upgrade of `awf_list_workspace_events` is a breaking change.** The current tool returns `list[dict] | None`; upgrading to `StructuredToolResult` with `WorkspaceEventListResponse` changes the response shape. This is consistent with the `awf_list_workspace_operations` migration (which already happened) and the MCP reference doc's migration note pattern. The parity matrix already documents this as `MCP partial`, so the upgrade is the intended resolution.
- **No cursor pagination yet.** The REST endpoints and repository don't implement cursor-based pagination for events (unlike operations). The MCP tool will match the REST behavior: `has_more` is computed via the `limit + 1` trick, but `cursor`/`next_cursor` will be `None`. This matches the existing `WorkspaceEventListResponse` schema which has the fields reserved but not yet populated.
- **`WorkspaceEventRepository.list()` already supports `workspace_id=None`** for global queries, so no repository changes are needed.
- **The global REST endpoint `GET /v1/events` does not filter by `event_type`**, but the repository does. The MCP tool should expose `event_type` filtering for parity with the workspace-scoped endpoint, making the MCP tool strictly more useful than the REST endpoint. This is acceptable — MCP can be a superset of REST filters.

## Explicit Non-Goals

- REST auth parity for `GET /v1/events` (the route has no explicit auth dependency; that's out of scope).
- CLI `awf events list` command parity (task only covers MCP).
- Cursor-based pagination for events (reserved for a later slice).
- Provider readiness, PR monitor, scheduler, console, or any other P1/P2 slice.
- Coverage policy changes.
- Changes to `awf_create_workspace`, `awf_create_workspace_v2`, or any control tools.
