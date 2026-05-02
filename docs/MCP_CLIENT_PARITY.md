# AWF API, CLI, and MCP Client Parity

This document is the client-surface inventory for AWF operator and agent
orchestrator access. It is a contract and backlog index only; this slice does
not add MCP tools.

## Role Contract

- REST is the canonical AWF control-plane API and schema source of truth.
- CLI is a JSON-first operator convenience layer over REST and local service
  diagnostics.
- MCP is a first-class parity client for agent orchestrators that need typed
  tool calls instead of shelling out to curl, Docker, or the AWF CLI.

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

| Capability | Canonical REST surface | CLI surface | MCP surface | Status | Notes and backlog |
| --- | --- | --- | --- | --- | --- |
| Workspace create, list, and get | `POST /v1/workspaces`, `POST /v2/workspaces`, `GET /v1/workspaces`, `GET /v1/workspaces/{workspace_id}` | `awf workspace create`, `awf workspace list`, `awf workspace show` | `awf_create_workspace`, `awf_create_workspace_v2`, `awf_list_workspaces`, `awf_get_workspace`, `awf_wait_for_workspace` | MCP implemented | REST remains canonical for request/response schemas. CLI currently submits v2 create requests. |
| Workspace overview | `GET /v1/workspaces/overview` | CLI absent | `awf_list_workspace_overview` | MCP implemented | Console and MCP use the operator overview envelope. |
| Merge queue | `GET /v1/merge-queue` | CLI absent | `awf_list_merge_queue` | MCP implemented | Merge readiness, blockers, freshness, and candidate state come from REST. |
| Task attempts | `GET /v1/tasks`, `GET /v1/tasks/{task_ref}/attempts` | CLI absent | `awf_list_tasks`, `awf_list_task_attempts` | MCP implemented | Useful for orchestrators tracking task lineage across retries. |
| Validation provenance | `GET /v1/workspaces/{workspace_id}/validation` | CLI absent | `awf_list_workspace_validation` | MCP implemented | Includes validation tier and freshness provenance from REST. |
| Stale reasons | `GET /v1/workspaces/{workspace_id}/stale-reasons` | CLI absent | `awf_list_workspace_stale_reasons` | MCP implemented | MCP should keep exposing reason codes and required actions, not log-derived summaries. |
| Artifact metadata | `GET /v1/workspaces/{workspace_id}/artifacts` | CLI absent | `awf_list_workspace_artifacts` | MCP implemented | Metadata only. This is intentionally not general filesystem browsing. |
| Artifact content/download | `GET /v1/workspaces/{workspace_id}/artifacts/download?path=...` | CLI absent | No `awf_read_workspace_artifact` or download tool | MCP missing/backlog | `artifact content/download` is backlog if MCP gets a bounded content tool. It must reuse AWF artifact path validation, size limits, and audit semantics. |
| Failure analysis metrics | `GET /v1/metrics/failures/summary` | CLI absent | `awf_get_failure_analysis_summary` | MCP implemented | Read-only operator metrics. |
| Workspace reliability metrics | `GET /v1/metrics/workspaces/summary` | CLI absent | `awf_get_workspace_reliability_summary` | MCP implemented | Read-only operator metrics. |
| Resource saturation metrics | `GET /v1/metrics/resources/saturation` | CLI absent | `awf_get_resource_saturation_summary` | MCP implemented | Includes cleanup readiness and admission pressure summaries. |
| SLO metrics | `GET /v1/metrics/slo` | CLI absent | `awf_get_slo_metrics_summary` | MCP implemented | Read-only operator metrics. |
| Locks and owned-path reservations | `GET /v1/locks` | `awf locks list` | `awf_list_locks` | MCP implemented | Owned paths are advisory coordination and stale-detection hints, not ordinary admission blockers. |
| Advisory overlap graph | `GET /v1/locks/overlap-graph` | CLI absent | `awf_get_overlap_graph` | MCP implemented | Shows active and queued overlap risk for orchestration decisions. |
| Service health and readiness | `GET /healthz`, `GET /readyz` | `awf service status`, `awf service doctor` | `awf_get_service_health`, `awf_get_service_readiness` | MCP implemented | CLI commands include local diagnostics; REST and MCP expose liveness/readiness snapshots. |
| Workspace runtime snapshot | `GET /v1/workspaces/{workspace_id}/runtime` | `awf workspace runtime` | `awf_get_workspace_runtime` | MCP implemented | Snapshot of AWF-managed compose/container state only. It is not Docker exec. |
| Workspace operations | `GET /v1/workspaces/{workspace_id}/operations` | `awf workspace operations` | `awf_list_workspace_operations` | MCP implemented | Per-workspace active and completed operations. |
| Global operations | `GET /v1/operations`, `GET /v1/operations/{operation_id}` | CLI absent | `awf_list_operations`, `awf_get_operation` | MCP implemented | Global operation audit view. |
| Durable workspace logs | `GET /v1/workspaces/{workspace_id}/logs`, `GET /v1/workspaces/{workspace_id}/logs/{stream_id}` | `awf workspace logs`, `awf workspace log` | `awf_list_workspace_logs`, `awf_read_workspace_log` | MCP implemented | Bounded reads from AWF-managed durable log streams only. |
| Workspace events | `GET /v1/events`, `GET /v1/workspaces/{workspace_id}/events` | `awf workspace events` for per-workspace events | `awf_list_workspace_events` | MCP partial | MCP lacks a global `awf_list_events` equivalent for `GET /v1/events`. |
| Cancel workspace | `POST /v1/workspaces/{workspace_id}/cancel` | CLI absent | `awf_cancel_workspace` | MCP partial | MCP has idempotency keys but no `If-Match` or expected-version argument yet. |
| Stop workspace stack | `POST /v1/workspaces/{workspace_id}/stop` | CLI absent | `awf_stop_workspace` | MCP partial | MCP has idempotency keys but no `If-Match` or expected-version argument yet. |
| Destroy workspace resources | `DELETE /v1/workspaces/{workspace_id}` | CLI absent | `awf_destroy_workspace` | MCP partial | MCP has idempotency keys but no `If-Match` or expected-version argument yet. |
| Remonitor workspace | `POST /v1/workspaces/{workspace_id}/remonitor` | `awf workspace remonitor` | `awf_remonitor_workspace` | MCP partial | MCP has idempotency keys but no `If-Match` or expected-version argument yet. |
| Request validation | `POST /v1/workspaces/{workspace_id}/validate` | CLI absent | `awf_request_workspace_validation` | MCP partial | MCP has idempotency keys but no `If-Match` or expected-version argument yet. |
| Refresh workspace | `POST /v1/workspaces/{workspace_id}/refresh` | CLI absent | No `awf_refresh_workspace` | MCP missing/backlog | Add only as an audited control-plane operation, not as git or shell access. |
| Rebase workspace | `POST /v1/workspaces/{workspace_id}/rebase` | CLI absent | No `awf_rebase_workspace` | MCP missing/backlog | Add only as an audited control-plane operation that preserves validation provenance. |
| Retry workspace | `POST /v1/workspaces/{workspace_id}/retry` | `awf workspace retry` | No `awf_retry_workspace` | MCP missing/backlog | Retry creates a fresh attempt and must preserve lineage and policy details. |
| Optimistic concurrency on controls | `If-Match` header on REST cancel, stop, destroy, remonitor, refresh, validate, and rebase | `--if-match` exists for `awf workspace remonitor` | No common expected-version argument on MCP controls | MCP partial | `If-Match` parity is backlog for mutating MCP tools. Idempotency keys alone do not replace version checks. |
| Live workspace stream | `WebSocket /v1/workspaces/{workspace_id}/ws` | CLI absent | No streaming MCP tool | Out of scope | MCP tools should prefer bounded snapshots and log reads over long-lived stream transport. |
| Secret lease status | `GET /v1/workspaces/{workspace_id}/secret-leases` | CLI absent | No MCP tool | Out of scope | Secret and credential material must not flow through MCP responses. |

## MCP Security Boundary

MCP may expose AWF-managed runtime snapshots, durable logs, artifact metadata,
bounded artifact content if explicitly implemented later, operations, metrics,
health, readiness, and audited safe control-plane operations.

MCP must expose AWF controls, not arbitrary shell. It must not provide
unrestricted Docker exec, raw container exec, host filesystem browsing,
unbounded artifact reads, credential dumps, token values, secret lease material,
or a generic command runner. Any future MCP content-read or control tool must
preserve the same authorization, idempotency, concurrency, audit, path
validation, and provenance semantics as the REST endpoint it mirrors.
