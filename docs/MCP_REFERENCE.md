# MCP Reference

## MCP Surface

AWF also exposes MCP tools for clients that want typed tool calls instead of
shelling out to the REST API. REST is canonical, the CLI is a JSON-first
operator convenience layer, and MCP is a first-class parity client for agent
orchestrators. See [MCP_CLIENT_PARITY.md](MCP_CLIENT_PARITY.md) for
the API/CLI/MCP parity matrix and explicit MCP backlog surfaces.

| Tool | Purpose |
| --- | --- |
| `awf_create_workspace` | Create a legacy v1 workspace request. |
| `awf_create_workspace_v2` | Create a profile-driven v2 workspace request. |
| `awf_get_workspace` | Fetch one workspace by id. |
| `awf_list_workspaces` | List recent workspaces newest-first. |
| `awf_wait_for_workspace` | Poll until a workspace reaches a terminal state or times out. |
| `awf_get_workspace_runtime` | Fetch one workspace's compose/container runtime snapshot. |
| `awf_list_merge_queue` | List the REST merge queue envelope for operator review. |
| `awf_list_workspace_overview` | List the REST workspace overview envelope. |
| `awf_list_workspace_validation` | List validation provenance for one workspace. |
| `awf_list_workspace_stale_reasons` | List active or resolved stale reasons for one workspace. |
| `awf_list_workspace_artifacts` | List workspace artifact metadata without reading artifact contents. |
| `awf_get_failure_analysis_summary` | Fetch the failure-analysis metrics summary. |
| `awf_get_workspace_reliability_summary` | Fetch the workspace reliability metrics summary. |
| `awf_get_resource_saturation_summary` | Fetch resource saturation, cleanup readiness, and admission status. |
| `awf_get_slo_metrics_summary` | Fetch the SLO metrics summary. |
| `awf_get_core_release_readiness` | Fetch the executable AWF Core release scorecard. |
| `awf_list_operations` | List operations globally with REST-compatible filters. |
| `awf_get_operation` | Fetch one operation by id. |
| `awf_list_workspace_operations` | List one workspace's active and completed operations newest-first. |
| `awf_list_workspace_events` | List one workspace's immutable events newest-first, with optional event-type filtering. |
| `awf_list_workspace_logs` | List indexed durable log streams for one workspace. |
| `awf_read_workspace_log` | Read a bounded log chunk by stream id and byte offset. |
| `awf_get_overlap_graph` | Fetch the advisory owned-path overlap graph. |
| `awf_list_tasks` | List task records backed by workspace attempts. |
| `awf_list_task_attempts` | List attempts for one task reference. |
| `awf_list_locks` | List owned-path reservations and overlap-risk summaries. |
| `awf_get_service_readiness` | Fetch service readiness checks. |
| `awf_get_service_health` | Fetch service liveness. |
| `awf_cancel_workspace` | Operator control: request cancellation for a workspace. |
| `awf_stop_workspace` | Operator control: stop a workspace stack. |
| `awf_destroy_workspace` | Operator control: destroy AWF-managed workspace resources. |
| `awf_remonitor_workspace` | Operator control: request PR monitor recovery. |
| `awf_request_workspace_validation` | Operator control: request workspace re-validation. |

The observability tools return `null` for a missing workspace, log stream, or
operation rather than surfacing raw storage errors. Operator observability tools
are read-only and mirror REST response envelopes; the explicit control tools do
not provide shell access or arbitrary Docker execution. Known MCP parity backlog
is documented in the matrix, including refresh, rebase, retry, artifact
content/download, and `If-Match` concurrency coverage.

Example `awf_create_workspace_v2` arguments:

```json
{
  "repo_url": "git@github.com:example/app.git",
  "base_branch": "main",
  "task_title": "Implement feature",
  "task_prompt": "Build the requested feature and commit the result.",
  "task_kind": "feature_branch_pr",
  "task_class": "docs_task",
  "owned_paths": ["README.md", "docs/**"],
  "agent": "codex",
  "model": null,
  "task_external_id": "AIRA-123",
  "profile_ref": "auto",
  "profile": null,
  "validation_commands": ["pytest -q"],
  "requested_tier": 1,
  "auto_merge": true,
  "initial_review_grace_period_seconds": null
}
```

Example runtime and operation observability calls:

`awf_get_workspace_runtime` arguments:

```json
{"workspace_id": "ws_abc123"}
```

`awf_list_workspace_operations` arguments:

```json
{"workspace_id": "ws_abc123", "limit": 25}
```

