# Validation: MCP Global Events Parity (P1)

## Plan reference
`plans/ws_32a3971e4aa147c08ed46683.md`

## Summary

Implemented MCP parity for global AWF events (`GET /v1/events`) by adding the `awf_list_events` tool and upgrading `awf_list_workspace_events` to return a `WorkspaceEventListResponse` envelope. Both capabilities are now `MCP implemented`.

## Changes made

### Source
1. **`src/awf/mcp/server.py`**
   - Added `awf_list_events` tool: global event listing with optional `workspace_id`, `event_type`, and `limit` filters, returning `StructuredToolResult` with `WorkspaceEventListResponse` envelope + `has_more` via `limit+1` trick.
   - Upgraded `awf_list_workspace_events`: return type changed from `list[dict[str, Any]] | None` to `CallToolResult`; returns `WorkspaceEventListResponse` envelope with `has_more`, `limit`, `cursor`, `next_cursor`; missing workspace returns `nullable` tool result (`_null_tool_result()`).

2. **`src/awf/service/workspaces.py`**
   - Added `list_global_events` method: accepts `workspace_id: str | None`, `event_type: str | None`, `limit: int = 50`, returns `list[WorkspaceEventResponse]`.

### Tests
3. **`tests/unit/mcp/test_mcp_server.py`**
   - Replaced `TestWorkspaceEvents::test_lists_requested_workspace_events_with_limit_and_type` with `test_lists_workspace_events_with_envelope_and_has_more` (envelope shape validation, `has_more=True` with limit truncation, `has_more=False` with large limit).
   - Replaced `TestWorkspaceEvents::test_missing_workspace_events_return_none` with `test_missing_workspace_events_return_null_tool_result` (returns `CallToolResult` with `structuredContent=None`).
   - Added `TestGlobalEvents` class with 6 tests:
     - `test_list_events_returns_empty_list`
     - `test_list_events_returns_events_across_workspaces`
     - `test_list_events_filters_by_workspace_id`
     - `test_list_events_filters_by_event_type`
     - `test_list_events_respects_limit`
     - `test_list_events_limit_bounds`
   - Added `awf_list_events` to `TestToolRegistration::test_existing_and_observability_tools_registered`.
   - Added `awf_list_events` and `awf_list_workspace_events` schema contract assertions in `test_operator_parity_tool_argument_contracts`.

4. **`tests/unit/mcp/test_mcp_operator_surfaces.py`**
   - Added `awf_list_events` to `NEW_OPERATOR_TOOLS` and `BOUNDED_READ_ONLY_LIST_TOOLS`.
   - Added `("global_events", "/v1/events", ...)` to `test_empty_read_only_operator_surfaces_match_rest_payloads`.

5. **`tests/unit/mcp/test_mcp_client_parity_docs.py`**
   - Added `"Workspace events": {"awf_list_events", "awf_list_workspace_events"}` to `READ_ONLY_OPERATOR_ROWS`.

6. **`tests/unit/contracts/_capabilities.py`**
   - `global_events`: `mcp_tool="awf_list_events"`, `parity_status="MCP implemented"`, `parity_backlog_slice="—"`, added `mcp_request_fields` and `mcp_required_fields`.
   - `workspace_events`: `parity_status="MCP implemented"`, `parity_backlog_slice="—"`, added `mcp_request_fields` reflecting envelope.

7. **`tests/unit/contracts/test_registry_smoke.py`**
   - Added `"Workspace events"` to `IMPLEMENTED_PARITY_COVERAGE_REFERENCES` with test references.

8. **`tests/unit/contracts/test_response_payload_alignment.py`**
   - Added `workspace_events` and `global_events` to `_read_rest_params` and `_read_mcp_args`.

### Docs
9. **`docs/MCP_CLIENT_PARITY.md`**
   - Updated "Workspace events" row: both `awf_list_events` and `awf_list_workspace_events` listed; status changed to `MCP implemented`; backlog slice `—`; security boundary updated to remove "no global event tool" language.

10. **`docs/MCP_REFERENCE.md`**
    - Added `awf_list_events` row.
    - Updated `awf_list_workspace_events` description to note envelope return.

11. **`TODO/pre-gke-industrial-readiness.md`**
    - Checked off `- [x] TODO§P1-mcp-global-events`.

## Validation results

```
$ uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py tests/unit/mcp/test_mcp_operator_surfaces.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py -q
157 passed in 104.93s

$ uv run --python 3.12 --extra dev ruff check src/awf tests
All checks passed!

$ uv run --python 3.12 --extra dev mypy src/awf
Success: no issues found in 153 source files

$ uv run --python 3.12 --extra dev pytest tests/unit/mcp tests/unit/contracts -q
687 passed in 242.74s

$ python scripts/generate_openapi.py --check
OK: openapi.json matches the current app spec.
```

## Gaps

None identified. All plan items are implemented and validated.
