# MCP Reference

## MCP Surface

AWF also exposes MCP tools for clients that want typed tool calls instead of
shelling out to the REST API. REST is canonical, the CLI is a JSON-first
operator convenience layer, and MCP is a first-class parity client for agent
orchestrators. See [MCP_CLIENT_PARITY.md](MCP_CLIENT_PARITY.md) for
the API/CLI/MCP parity matrix and explicit MCP backlog surfaces. See
[MCP_SETUP.md](MCP_SETUP.md) for Claude Code and Codex setup snippets using
`awf mcp serve`.

| Tool | Purpose |
| --- | --- |
| `awf_create_workspace` | Create a profile-driven workspace request. |
| `awf_adopt_pull_request_monitor` | Adopt an already-open GitHub PR into AWF monitoring without rerunning the original coding agent. |
| `awf_get_workspace` | Fetch one workspace by id. |
| `awf_list_workspaces` | List recent workspaces newest-first, optionally filtered by status, agent, or repo URL. |
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
| `awf_list_events` | Read-only operator global event listing, supporting optional workspace and event-type filters. |
| `awf_list_workspace_events` | List one workspace's immutable events newest-first, returning a `WorkspaceEventListResponse` envelope. |
| `awf_list_workspace_logs` | List indexed durable log streams for one workspace. |
| `awf_read_workspace_log` | Read a bounded log chunk by stream id and byte offset. |
| `awf_get_overlap_graph` | Fetch the advisory owned-path overlap graph. |
| `awf_list_tasks` | List task records backed by workspace attempts. |
| `awf_list_task_attempts` | List attempts for one task reference. |
| `awf_list_locks` | List owned-path reservations and overlap-risk summaries. |
| `awf_get_service_readiness` | Fetch service readiness checks. |
| `awf_get_service_health` | Fetch service liveness. |
| `awf_get_setup_status` | Run read-only first-run setup status and return safe status/ref metadata only. |
| `awf_start_local_service` | Start local AWF Core through the existing idempotent bootstrap engine. |
| `awf_initialize_project_profile` | Preview or write `.awf/workspace.yml` with the same onboarding writer as `awf init`. |
| `awf_get_client_integration_instructions` | Return secret-free Claude/Codex MCP client integration instructions. |
| `awf_cancel_workspace` | Operator control: request cancellation for a workspace. |
| `awf_stop_workspace` | Operator control: stop a workspace stack. |
| `awf_destroy_workspace` | Operator control: destroy AWF-managed workspace resources. |
| `awf_remonitor_workspace` | Operator control: request PR monitor recovery. |
| `awf_guide_workspace` | Operator control: inject a directive into a live monitoring workspace (closes the NotifyHuman loop). |
| `awf_request_workspace_validation` | Operator control: request workspace re-validation. |
| `awf_refresh_workspace` | Operator control: refresh workspace state after upstream changes. |
| `awf_rebase_workspace` | Operator control: rebase workspace work onto the current base. |
| `awf_retry_workspace` | Retry a failed or cancelled workspace as a fresh attempt. |

The observability tools return `null` for a missing workspace, log stream, or
operation rather than surfacing raw storage errors. Operator observability tools
are read-only and mirror REST response envelopes; the explicit control tools do
not provide shell access or arbitrary Docker execution. The parity matrix records
implemented REST/CLI/MCP surfaces and the few surfaces intentionally out of
scope for MCP.

The create tool `awf_create_workspace` accepts a schema-optional
`idempotency_key` argument. Reusing the same key with the same effective create
payload returns the existing workspace; reusing the key with a changed payload
returns structured `IDEMPOTENCY_CONFLICT`.

**MCP control migration note:** The control tools `awf_cancel_workspace`,
`awf_stop_workspace`, `awf_destroy_workspace`, `awf_remonitor_workspace`,
`awf_guide_workspace`, `awf_request_workspace_validation`,
`awf_refresh_workspace`, and `awf_rebase_workspace` have a required
`idempotency_key` argument. Existing MCP
clients that omitted this argument or sent `null` must pass a stable non-empty
key for each operator action. This mirrors the REST `Idempotency-Key`
requirement for the same control routes; `expected_version` remains optional and
maps to `If-Match`.

**MCP log and operation response migration note:** `awf_read_workspace_log`
now returns `WorkspaceLogReadResponse` with `stream_id`, `offset`,
`next_offset`, `eof`, and `data`; clients should read log content from `data`
instead of the previous raw `text` key. `awf_list_workspace_logs` now returns
the `WorkspaceLogListResponse` envelope, and `awf_list_workspace_operations`
now returns the `OperationListResponse` envelope. Clients that consumed the old
top-level lists should iterate `items` and honor `has_more`, `limit`, and
`cursor`.

**First-run setup tools:** `awf_get_setup_status` returns setup status,
selected provider names, check levels, provider/client status, and credential
reference metadata such as whether a ref exists and its scheme. It never
returns raw credential values or env-file contents. `awf_start_local_service`
delegates to the same bootstrap engine as `awf start`; repeated calls are
allowed and failures return structured first-run payloads. `awf_initialize_project_profile`
uses the onboarding preview/writer shared with `awf init`. `awf_get_client_integration_instructions`
builds Claude/Codex plans from the same client descriptors as `awf setup
--client` and returns command/args/config-path instructions without reading
the referenced env file.

Example `awf_create_workspace` arguments:

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
  "effort": null,
  "task_external_id": "AIRA-123",
  "profile_ref": "auto",
  "profile": null,
  "validation_commands": ["pytest -q"],
  "requested_tier": 1,
  "auto_merge": true,
  "initial_review_grace_period_seconds": null,
  "companions": [
    {
      "name": "backend",
      "repo_url": "git@github.com:example/api.git",
      "base_branch": "main",
      "build_context": ".",
      "dockerfile": "Dockerfile",
      "env_file": "config/dev.env",
      "compose_up_timeout_seconds": 900,
      "depends_on": ["docker"],
      "healthcheck_cmd": "curl -fsS http://localhost:8000/health"
    }
  ],
  "idempotency_key": "example-task-001"
}
```

`companions` is optional. Each item is the same object accepted by
`POST /v1/workspaces`: AWF clones the repo into a managed companion worktree,
resolves declared paths inside that checkout, and adds the service to the
workspace Compose stack. Set `compose_up_timeout_seconds` on a companion when
its build/start path needs a longer timeout than the profile default. Do not
pass raw host paths or local secret files.

Example adoption and observability calls:

Example `awf_adopt_pull_request_monitor` arguments:

```json
{
  "repo_slug": "owner/repo",
  "pr_number": 123,
  "auto_merge": true,
  "initial_review_grace_period_seconds": 900,
  "reason": "attach AWF to existing PR"
}
```

Adoption maps to `POST /v1/workspaces/adopt-pr` and returns
`PullRequestMonitorAdoptionResponse`. AWF derives deterministic repo/PR
idempotency; callers do not provide an `Idempotency-Key`. See
[PR Monitor Adoption](PR_MONITOR_ADOPTION.md) for GitHub auth readiness,
monitor policy, terminal adoption retry behavior, console inspection, and the
mocked-local demo path.

`awf_get_workspace_runtime` arguments:

```json
{"workspace_id": "ws_abc123"}
```

`awf_list_workspace_operations` arguments:

```json
{"workspace_id": "ws_abc123", "limit": 25, "status": "running", "type": "validate"}
```

**Breaking change:** `awf_list_workspace_operations` now returns a
`CallToolResult` whose structured content is the REST-compatible
`OperationListResponse` envelope, not a flat JSON array. Existing MCP clients
must read operation rows from `items` and handle the pagination fields:

```json
{
  "items": [],
  "has_more": false,
  "next_cursor": null,
  "limit": 25,
  "cursor": null
}
```
