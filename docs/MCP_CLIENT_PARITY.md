# AWF API, CLI, and MCP Client Parity

This document is the client-surface inventory for AWF operator and agent
orchestrator access. It is both an implementation contract for shipped client
surfaces and a backlog index for explicit parity gaps.

## Role Contract

- REST is the canonical AWF control-plane API and schema source of truth.
- CLI is a JSON-first operator convenience layer over REST and local service
  diagnostics.
- MCP is a first-class parity client for agent orchestrators that need typed
  tool calls instead of shelling out to curl, Docker, or the AWF CLI.

**Note on SDKs (v0.1):** REST, CLI, and MCP are the supported client surfaces for v0.1. AWF does not ship with a supported Python SDK. Integrators must use one of the supported surfaces. Do not import internal AWF modules to build custom API clients.

**MCP control migration note:** The MCP control tools `awf_cancel_workspace`,
`awf_stop_workspace`, `awf_destroy_workspace`, `awf_remonitor_workspace`,
`awf_request_workspace_validation`, `awf_refresh_workspace`, and
`awf_rebase_workspace` have a required `idempotency_key` argument. Existing MCP clients that
omitted this argument or sent `null` must pass a stable non-empty key for each
operator action. This mirrors the REST `Idempotency-Key` requirement for the
same control routes; `expected_version` remains optional and maps to `If-Match`.

**MCP create idempotency note:** `awf_create_workspace` accepts a
schema-optional `idempotency_key` argument.
The key maps to REST `Idempotency-Key`: same key and same effective create
payload returns the existing workspace, while same key and changed payload
returns structured `IDEMPOTENCY_CONFLICT`.

**MCP create effort field note:** The agent `effort` field is available on
`awf_create_workspace`, `awf workspace create`, and the corresponding workspace
create REST surface. When omitted, AWF resolves the provider-specific default
from the workspace profile or adapter defaults.

**MCP log and operation response migration note:** MCP log and operation tools
now use REST-compatible response models. `awf_read_workspace_log` returns
`WorkspaceLogReadResponse`, so clients should read log content from `data`
instead of the previous raw `text` key. `awf_list_workspace_logs` returns a
`WorkspaceLogListResponse` envelope, and `awf_list_workspace_operations`
returns an `OperationListResponse` envelope; clients should iterate `items`
and honor `has_more`, `limit`, and `cursor` instead of treating the result as a
top-level list.

**MCP first-run setup note:** `awf_get_setup_status`,
`awf_start_local_service`, `awf_initialize_project_profile`, and
`awf_get_client_integration_instructions` are local first-run MCP controls.
They mirror CLI setup/start/init/client intent rather than adding REST routes.
They do not accept credential-value inputs, do not read env-file contents into
responses, and return only status, paths, command arguments, and safe
credential-reference metadata.

## Status Vocabulary

- `MCP implemented`: MCP exposes the same operator data or control intent as
  REST, with REST-compatible response envelopes where applicable.
- `MCP partial`: MCP exposes a useful subset, but a named REST behavior is not
  covered yet.
- `MCP missing/backlog`: REST or CLI exposes the surface and MCP has no
  equivalent tool yet.
- `CLI absent`: no dedicated CLI command exists; use REST or MCP directly.
- `Out of scope`: not planned for MCP because it conflicts with the safety or
  transport model.

## Parity Matrix

| Capability | Canonical REST surface | CLI surface | MCP tool name | Schema / Error-Code Contract | Security Boundary | Status | Backlog Slice |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Workspace create | `POST /v1/workspaces` | `awf workspace create` | `awf_create_workspace` | `WorkspaceAcceptedResponse`; IDEMPOTENCY_CONFLICT, INVALID_PROFILE, TASK_EXTERNAL_ID_CONFLICT, INSUFFICIENT_DISK | `require_api_token`; MCP create accepts optional `idempotency_key`; no shell, no exec | MCP implemented | — |
| Workspace list and get | `GET /v1/workspaces`, `GET /v1/workspaces/{workspace_id}` | `awf workspace list`, `awf workspace show` | `awf_list_workspaces`, `awf_get_workspace`, `awf_wait_for_workspace` | `WorkspaceResponse` | `require_api_token`; MCP bounded reads only | MCP implemented | — |
| Existing PR monitor adoption | `POST /v1/workspaces/adopt-pr` | `awf workspace adopt-pr --repo owner/repo --pr 123`; `awf workspace adopt-pr --pr-url https://github.com/owner/repo/pull/123` | `awf_adopt_pull_request_monitor` | `PullRequestMonitorAdoptionResponse`; PR_ADOPTION_INPUT_REQUIRED, INVALID_GITHUB_REPO, PR_NOT_FOUND, PR_ALREADY_CLOSED, PR_ALREADY_MERGED, PR_METADATA_FETCH_FAILED, PR_METADATA_INVALID, PR_ADOPTION_POLICY_CONFLICT | `require_api_token`; MCP: audited control-plane adoption only, no shell, no exec | MCP implemented | — |
| Workspace overview | `GET /v1/workspaces/overview` | CLI absent | `awf_list_workspace_overview` | `WorkspaceOverviewListResponse` | `require_api_token` | MCP implemented | — |
| Merge queue | `GET /v1/merge-queue` | CLI absent | `awf_list_merge_queue` | `MergeQueueListResponse` | `require_api_token` | MCP implemented | — |
| Task attempts | `GET /v1/tasks`, `GET /v1/tasks/{task_ref}/attempts` | CLI absent | `awf_list_tasks`, `awf_list_task_attempts` | `TaskListResponse`; `TaskAttemptListResponse` | `require_api_token` | MCP implemented | — |
| Validation provenance | `GET /v1/workspaces/{workspace_id}/validation` | CLI absent | `awf_list_workspace_validation` | `ValidationProvenanceListResponse` | `require_api_token` | MCP implemented | — |
| Stale reasons | `GET /v1/workspaces/{workspace_id}/stale-reasons` | CLI absent | `awf_list_workspace_stale_reasons` | `StaleReasonListResponse` | `require_api_token` | MCP implemented | — |
| Artifact metadata | `GET /v1/workspaces/{workspace_id}/artifacts` | CLI absent | `awf_list_workspace_artifacts` | `WorkspaceArtifactListResponse` | `require_api_token` | MCP implemented | — |
| Artifact content/download | `GET /v1/workspaces/{workspace_id}/artifacts/download` | CLI absent | `awf_read_workspace_artifact` | `WorkspaceArtifactReadResponse`; INVALID_ARTIFACT_PATH, NOT_FOUND, ARTIFACT_OVERSIZED, ARTIFACT_BLOCKED | `require_api_token`; MCP: bounded content only, size limits enforced | MCP implemented | — |
| Failure analysis metrics | `GET /v1/metrics/failures/summary` | CLI absent | `awf_get_failure_analysis_summary` | `FailureAnalysisSummaryResponse` | `require_api_token` | MCP implemented | — |
| Workspace reliability metrics | `GET /v1/metrics/workspaces/summary` | CLI absent | `awf_get_workspace_reliability_summary` | `WorkspaceReliabilitySummaryResponse` | `require_api_token` | MCP implemented | — |
| Resource saturation metrics | `GET /v1/metrics/resources/saturation` | CLI absent | `awf_get_resource_saturation_summary` | `ResourceSaturationSummaryResponse` | `require_api_token` | MCP implemented | — |
| SLO metrics | `GET /v1/metrics/slo` | CLI absent | `awf_get_slo_metrics_summary` | `SloMetricsSummaryResponse` | `require_api_token` | MCP implemented | — |
| Locks and owned-path reservations | `GET /v1/locks` | `awf locks list` | `awf_list_locks` | `WorkspaceLockListResponse` | `require_api_token` | MCP implemented | — |
| Advisory overlap graph | `GET /v1/locks/overlap-graph` | CLI absent | `awf_get_overlap_graph` | `WorkspaceOverlapGraphResponse` | `require_api_token` | MCP implemented | — |
| Service health and readiness | `GET /healthz`, `GET /readyz` | `awf service status`, `awf service doctor` | `awf_get_service_health`, `awf_get_service_readiness` | `HealthResponse`; `ReadyResponse` | healthz, readyz: public; `require_api_token` for other operator routes | MCP implemented | — |
| Core release readiness scorecard | `GET /release-readiness` | `awf service readiness --format json` | `awf_get_core_release_readiness` | `CoreReadinessReport` | `require_api_token`; CLI may run local diagnostics and reuse cached service status | MCP implemented | — |
| Workspace runtime snapshot | `GET /v1/workspaces/{workspace_id}/runtime` | `awf workspace runtime` | `awf_get_workspace_runtime` | `WorkspaceRuntimeResponse` | `require_api_token`; MCP: no shell, no exec | MCP implemented | — |
| Workspace operations | `GET /v1/workspaces/{workspace_id}/operations` | `awf workspace operations --status --type --limit --cursor`, `awf workspace operations --after` | `awf_list_workspace_operations` | `OperationListResponse` | `require_api_token`; MCP: read-only, no shell, no exec | MCP implemented | — |
| Global operations | `GET /v1/operations`, `GET /v1/operations/{operation_id}` | `awf operations list --workspace-id --status --type --limit --cursor`, `awf operations list --after`, `awf operations show` | `awf_list_operations`, `awf_get_operation` | `OperationListResponse`; `OperationResponse` | `require_api_token`; MCP: read-only, no shell, no exec | MCP implemented | — |
| Durable workspace logs | `GET /v1/workspaces/{workspace_id}/logs`, `GET /v1/workspaces/{workspace_id}/logs/{stream_id}` | `awf workspace logs`, `awf workspace log` | `awf_list_workspace_logs`, `awf_read_workspace_log` | `WorkspaceLogListResponse`; `WorkspaceLogReadResponse` | `require_api_token`; MCP: bounded reads only | MCP implemented | — |
| Workspace events | `GET /v1/events`, `GET /v1/workspaces/{workspace_id}/events` | `awf workspace events` | `awf_list_events`, `awf_list_workspace_events` | `WorkspaceEventListResponse` | `require_api_token` | MCP implemented | — |
| Cancel workspace | `POST /v1/workspaces/{workspace_id}/cancel` | `awf workspace cancel --idempotency-key --if-match --reason --stop-stack` | `awf_cancel_workspace` | `WorkspaceControlResponse`; NOT_FOUND, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT | `require_api_token`; MCP: no shell, no exec, no credential dump | MCP implemented | — |
| Stop workspace stack | `POST /v1/workspaces/{workspace_id}/stop` | `awf workspace stop --idempotency-key --if-match --reason` | `awf_stop_workspace` | `WorkspaceControlResponse`; NOT_FOUND, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT, STACK_STOP_FAILED | `require_api_token`; MCP: no shell, no exec, no credential dump | MCP implemented | — |
| Destroy workspace resources | `DELETE /v1/workspaces/{workspace_id}` | `awf workspace destroy --idempotency-key --if-match --force --remove-volumes --remove-worktree` | `awf_destroy_workspace` | `WorkspaceControlResponse`; NOT_FOUND, WORKSPACE_ACTIVE, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT, STACK_STOP_FAILED | `require_api_token`; MCP: no shell, no exec, no credential dump | MCP implemented | — |
| Remonitor workspace | `POST /v1/workspaces/{workspace_id}/remonitor` | `awf workspace remonitor --idempotency-key --if-match --reason` | `awf_remonitor_workspace` | `WorkspaceControlResponse`; NOT_FOUND, WORKSPACE_PR_URL_REQUIRED, WORKSPACE_STATE_NOT_REMONITORABLE, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT | `require_api_token`; MCP: no shell, no exec, no credential dump | MCP implemented | — |
| Request validation | `POST /v1/workspaces/{workspace_id}/validate` | `awf workspace validate --idempotency-key --if-match --reason --requested-tier` | `awf_request_workspace_validation` | `OperationResponse`; NOT_FOUND, WORKSPACE_PR_URL_REQUIRED, WORKSPACE_STATE_NOT_VALIDATABLE, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT | `require_api_token`; MCP: no shell, no exec, no credential dump | MCP implemented | — |
| Refresh workspace | `POST /v1/workspaces/{workspace_id}/refresh` | `awf workspace refresh --idempotency-key --if-match --reason` | `awf_refresh_workspace` | `OperationResponse`; NOT_FOUND, WORKSPACE_STATE_NOT_REFRESHABLE, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT | `require_api_token`; MCP: audited control-plane operation, not shell access | MCP implemented | — |
| Rebase workspace | `POST /v1/workspaces/{workspace_id}/rebase` | `awf workspace rebase --idempotency-key --if-match --reason` | `awf_rebase_workspace` | `OperationResponse`; NOT_FOUND, WORKSPACE_STATE_NOT_REBASEABLE, MERGE_CANDIDATE_NOT_FOUND, WORKSPACE_REBASE_CONFLICT, WORKSPACE_OPERATION_CONFLICT, VERSION_CONFLICT, IDEMPOTENCY_CONFLICT | `require_api_token`; MCP: audited control-plane operation, preserves validation provenance | MCP implemented | — |
| Retry workspace | `POST /v1/workspaces/{workspace_id}/retry` | `awf workspace retry` | `awf_retry_workspace` | `WorkspaceRetryResponse`; WORKSPACE_NOT_FOUND, WORKSPACE_NOT_RETRYABLE, WORKSPACE_RETRY_EXHAUSTED, WORKSPACE_RETRY_SALVAGE_UNAVAILABLE, PROVIDER_READINESS_PRECHECK_FAILED | `require_api_token`; MCP: preserves retry lineage and provider-readiness policy without shell access | MCP implemented | — |
| Optimistic concurrency on controls | `If-Match` header on REST cancel, stop, destroy, remonitor, refresh, validate, and rebase | `awf workspace cancel --if-match`, `awf workspace stop --if-match`, `awf workspace destroy --if-match`, `awf workspace remonitor --if-match`, `awf workspace refresh --if-match`, `awf workspace validate --if-match`, `awf workspace rebase --if-match` | `awf_cancel_workspace`, `awf_stop_workspace`, `awf_destroy_workspace`, `awf_remonitor_workspace`, `awf_request_workspace_validation`, `awf_refresh_workspace`, `awf_rebase_workspace` | `WorkspaceControlResponse`; `OperationResponse`; VERSION_CONFLICT | `require_api_token`; MCP: all 7 control tools require `idempotency_key` and expose optional `expected_version`; no shell, no exec, no credential dump | MCP implemented | — |
| Local first-run setup/start/init/client | Local first-run setup contract (REST unchanged) | `awf setup --dry-run`, `awf start`, `awf init`, `awf setup --client` | `awf_get_setup_status`, `awf_start_local_service`, `awf_initialize_project_profile`, `awf_get_client_integration_instructions` | `FirstRunPayload`; structured setup/start/init/client payloads | Local MCP only; no credential-value inputs; no env-file contents; setup status returns safe refs/status only | MCP implemented | — |
| Live workspace stream | `WebSocket /v1/workspaces/{workspace_id}/ws` | CLI absent | No streaming MCP tool | N/A (out of scope) | WebSocket excluded: MCP prefers bounded snapshots over streaming transport | Out of scope | — |
| Secret lease status | `GET /v1/workspaces/{workspace_id}/secret-leases` | CLI absent | No MCP tool | `WorkspaceSecretLeaseListResponse` | `require_api_token`; Secret and credential material must not flow through MCP responses; REST-only and intentionally out-of-scope for MCP | Out of scope | — |

## MCP Security Boundary

MCP may expose AWF-managed runtime snapshots, durable logs, artifact metadata,
bounded artifact content, operations, metrics, health, readiness, and audited
safe control-plane operations. MCP may also expose bounded local first-run
setup/start/init/client controls when those tools delegate to AWF's existing
setup/start/onboarding/client-planning helpers and do not carry raw credential
values.

MCP must expose AWF controls, not arbitrary shell. It must not provide
unrestricted Docker exec, raw container exec, host filesystem browsing,
unbounded artifact reads, credential dumps, token values, secret lease material,
or a generic command runner. Any future MCP content-read or control tool must
preserve the same authorization, idempotency, concurrency, audit, path
validation, and provenance semantics as the REST endpoint it mirrors.
