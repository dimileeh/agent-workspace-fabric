# REST API Reference

## API Surface

Run the API locally:

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

The OpenAPI spec is served at `/openapi.json` and browsable at `/docs`.
A checked-in stable copy is available as `openapi.json` in the repository root.

All endpoints return JSON. Endpoints requiring authentication use the
`Authorization: Bearer $AWF_API_TOKEN` header.

Public operators' health/readiness probes intentionally remain usable without
`AWF_API_TOKEN` so monitoring can run during bootstrapping. Workspace metadata
and control surfaces require the header and return a `503 API_TOKEN_NOT_CONFIGURED`
envelope when authentication is enabled but `AWF_API_TOKEN` is missing.

Common response patterns:

- `404` with `{"error_code": "NOT_FOUND", "message": "..."}` for missing resources
- `409` with `{"error_code": "...", "message": "..."}` for idempotency conflicts
- `422` for validation errors
- `503` when a dependency (DB, Docker) is unavailable

---

## Health and Readiness

### Liveness check

No auth required. Dependency-free probe that confirms the HTTP stack is up.

```bash
curl http://localhost:8000/healthz
```

Response shape:

```json
{"status": "ok", "service": "awf", "version": "0.1.0"}
```

### Readiness check

No auth required. Reports per-dependency health (DB, Docker CLI/daemon/Compose,
agent runtime image, orphan resources).

```bash
curl http://localhost:8000/readyz
```

Returns `200` when all checks pass, `503` when one or more fail.
Each sub-check includes a stable `reason` code for alert routing.

### Release readiness

No auth required. Returns the AWF Core local release scorecard.

```bash
curl http://localhost:8000/release-readiness
```

Returns `200` when the release is ready, `503` otherwise. Supports query params
`provider`, `failure_window_hours`, `slo_window_hours`.

```bash
curl "http://localhost:8000/release-readiness?provider=claude_code&provider=cursor"
```

The filtered example intentionally includes Cursor. Repeat `provider` to compare
any supported provider subset in one scorecard response.

---

## Create Workspace

### Create a workspace

Uses the canonical structured workspace request body. Supports idempotency via
`Idempotency-Key` header and provider readiness preflight.

```bash
curl -X POST http://localhost:8000/v1/workspaces \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-task-001" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -d '{
    "repo": {
      "url": "git@github.com:example/app.git",
      "base_branch": "main"
    },
    "task": {
      "title": "Implement feature",
      "prompt": "Build the requested feature and commit the result.",
      "kind": "feature_branch_pr",
      "agent": "codex",
      "model": null,
      "task_class": "refactor_task",
      "owned_paths": ["src/**", "tests/**"],
      "auto_merge": true,
      "initial_review_grace_period_seconds": null
    },
    "workspace": {
      "profile_ref": "auto",
      "profile": null
    },
    "validation": {
      "commands": ["pytest -q"],
      "requested_tier": 1
    },
    "resources": {
      "cpu": 4,
      "memory": "8g"
    }
  }'
```

Returns `202 Accepted` with workspace ID, status URL, and events URL.

The task object accepts policy metadata:

- `task_class`: one of `docs_task`, `test_task`, `refactor_task`,
  `migration_task`, `dependency_task`, or `build_config_task`.
- `owned_paths`: path globs the task expects to own; defaults to `[]`.

---

## List and Filter Workspaces

### Dashboard-friendly workspace overview

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/overview?status=monitoring_pr&agent=codex&limit=25"
```

### List workspaces (full detail)

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces?limit=50"
```

Filter by status, agent, or repo URL:

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces?status=monitoring_pr&agent=codex&repo_url=git@github.com:example/app.git&limit=25"
```

---

## Get Workspace Status

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123"
```

Returns the full workspace response including status, task policy, validation
provenance, lifecycle stages, LLM usage, and provider recovery state.

### Secret lease status (operator metadata)

Auth required (`Authorization: Bearer $AWF_API_TOKEN`).

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/secret-leases"
```

Returns redacted secret lease inventory (`lease_id`, `secret_name`, `target`, `provider`,
`status`, `expires_at`, etc.) for operator metadata visibility and control.

---

## Read Logs and Events

### List workspace events

Auth required (`Authorization: Bearer $AWF_API_TOKEN`).

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/events?workspace_id=ws_123&limit=50"
```

Events response shape:

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

### List workspace events (per-workspace)

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/events?limit=50"
```

### List workspace log streams

Auth required (`Authorization: Bearer $AWF_API_TOKEN`).

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/logs"
```

### Read a log stream

Auth required.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/logs/ls_abc?offset=0&limit_bytes=65536"
```

---

## Request Validation

Request a validation run for a workspace. Auth required.
Supports idempotency via `Idempotency-Key` and optimistic concurrency via
`If-Match` (workspace version).

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/validate" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: validate-ws-123-tier2" \
  -H "If-Match: 3" \
  -H "Content-Type: application/json" \
  -d '{"requested_tier": 2}'
```

Returns `202 Accepted` with an operation response.

---

## Remonitor

Re-enter PR monitoring for a workspace that had its monitor fall off.
Auth required. Requires `Idempotency-Key` and supports `If-Match`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/remonitor" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: remonitor-ws-123" \
  -H "If-Match: 7" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Monitor exited unexpectedly"}'
```

---

## Retry

Retry a terminal (failed/cancelled) workspace by creating a new workspace
that inherits repo, branch, and task configuration. Auth required when
`AWF_API_TOKEN` is configured.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/retry" \
  -H "Authorization: Bearer $AWF_API_TOKEN"
```

Optional query params: `provider_readiness_override`, `provider_readiness_override_reason`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/retry?provider_readiness_override=true&provider_readiness_override_reason=Capacity+recovered" \
  -H "Authorization: Bearer $AWF_API_TOKEN"
```

---

## Release Readiness

See Health and Readiness above. Evaluates SLO metrics, validation, and
dependency health to produce a local release scorecard.

```bash
curl http://localhost:8000/release-readiness
```

---

## Refresh and Rebase

### Refresh workspace

Pull the target branch into the workspace branch without replaying the agent.
Auth required. Requires `Idempotency-Key` and supports `If-Match`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/refresh" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: refresh-ws-123" \
  -H "If-Match: 5" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Target branch advanced"}'
```

### Rebase workspace

Rebase the workspace branch onto the current target branch tip.
Auth required. Requires `Idempotency-Key` and supports `If-Match`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/rebase" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: rebase-ws-123" \
  -H "If-Match: 8" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Stale after target merge"}'
```

---

## Cancel and Stop

### Cancel workspace

Mark a running workspace as cancelled and clean up resources.
Auth required. Requires `Idempotency-Key` and supports `If-Match`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/cancel" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: cancel-ws-123" \
  -H "If-Match: 2" \
  -H "Content-Type: application/json" \
  -d '{"reason": "No longer needed", "stop_stack": true}'
```

### Stop workspace

Stop the workspace container stack without marking the workspace as cancelled.
Auth required. Requires `Idempotency-Key` and supports `If-Match`.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/stop" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: stop-ws-123" \
  -H "If-Match: 3" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Agent stuck"}'
```

---

## Destroy

Permanently delete a workspace and its resources. Auth required.
Supports `Idempotency-Key` and `If-Match`.

```bash
curl -X DELETE "http://localhost:8000/v1/workspaces/ws_123" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: destroy-ws-123" \
  -H "If-Match: 5"
```

Query params: `force` (boolean), `remove_volumes` (boolean, default true),
`remove_worktree` (boolean, default true).

---

## Merge Queue

List merge candidates with their merge readiness, blockers, and stale reasons.

```bash
curl "http://localhost:8000/v1/merge-queue?limit=50"
```

Filter by repo, base branch, or workspace status:

```bash
curl "http://localhost:8000/v1/merge-queue?repo_url=git@github.com:example/app.git&base_branch=main&status=monitoring_pr"
```

---

## Validation Provenance

List validation run provenance for a workspace, including tier, freshness,
command set hash, and coverage status.

```bash
curl "http://localhost:8000/v1/workspaces/ws_123/validation?limit=20"
```

---

## Stale Reasons

List structured stale reasons for a workspace's merge candidate.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/stale-reasons?include_resolved=false&limit=20"
```

---

## Operations

### List all operations

```bash
curl "http://localhost:8000/v1/operations?limit=50"
curl "http://localhost:8000/v1/operations?limit=50&cursor=$NEXT_CURSOR"
```

Filter by workspace ID, status, or operation type:

```bash
curl "http://localhost:8000/v1/operations?workspace_id=ws_123&type=rebase&status=succeeded"
```

### List workspace operations

```bash
curl "http://localhost:8000/v1/workspaces/ws_123/operations?limit=50"
curl "http://localhost:8000/v1/workspaces/ws_123/operations?limit=50&cursor=$NEXT_CURSOR"
```

### Get a single operation

```bash
curl "http://localhost:8000/v1/operations/op_abc"
```

---

## Tasks

### List tasks

Workspace-backed task views with attempt and merge candidate status for operator
consoles.

```bash
curl "http://localhost:8000/v1/tasks?limit=50"
```

Filter by status and agent:

```bash
curl "http://localhost:8000/v1/tasks?status=monitoring_pr&agent=codex"
```

### List task attempts

```bash
curl "http://localhost:8000/v1/tasks/task_abc/attempts?limit=100"
```

---

## Artifacts

List and download workspace artifacts through the protected observability API.

### List artifacts

Auth required.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts"
```

### Download an artifact

Auth required. Supports `?path=` to download a specific file.

```bash
curl -OJ -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts/download?path=logs/stdout.txt"
```

Artifact downloads are limited to regular files under
`<AWF_WORK_DIR>/artifacts/<workspace_id>` using POSIX-style relative paths.
Absolute paths, traversal segments, backslashes, symlinks, and missing files are
rejected without reading arbitrary host paths.

---

## Callbacks

Register external HTTP callback targets for sanitized AWF event envelopes.

### Create a callback subscription

Auth required. Requires `Idempotency-Key`.

```bash
curl -X POST http://localhost:8000/v1/callbacks \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: callback-myapp-001" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "MyApp deploy notifier",
    "target_url": "https://myapp.example.com/awf/events",
    "event_types": ["workspace.state_changed", "workspace.secondary_failure_recorded"],
    "enabled": true,
    "timeout_seconds": 10,
    "max_attempts": 3,
    "initial_backoff_seconds": 5
  }'
```

### List callback subscriptions

Auth required.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/callbacks?enabled=true&limit=50"
```

Public callback subscriptions accept the wildcards `workspace.*`, `merge.*`,
and `operation.*`, plus exact public event types such as
`workspace.created`, `workspace.state_changed`,
`workspace.secondary_failure_recorded`, `operation.state_changed`, and
`merge.candidate_updated`.

Workspace callback deliveries use a sanitized envelope. For
`workspace.secondary_failure_recorded`, the public shape is the same workspace
event envelope used for other workspace events:

```json
{
  "event": {
    "kind": "workspace",
    "type": "workspace.secondary_failure_recorded",
    "source_id": "evt_01HXYZ",
    "occurred_at": "2026-05-14T12:00:00Z"
  },
  "workspace": {
    "id": "ws_01HXYZ",
    "old_state": "failed",
    "new_state": "failed",
    "reason_code": "PYTEST_TEST_FAILURE"
  },
  "delivery": {
    "id": "cbd_01HXYZ",
    "subscription_id": "cb_01HXYZ",
    "idempotency_key": "callback-delivery:cb_01HXYZ:workspace:evt_01HXYZ",
    "dedupe_key": "workspace:evt_01HXYZ",
    "attempt_count": 0,
    "max_attempts": 3
  }
}
```

The internal failure-causality payload keys stored on the workspace event, such
as `primary_failure`, `secondary_failure`, and `secondary_failures`, are not
part of the external callback envelope.

For each outbound delivery, callback target URLs are revalidated before the POST
is sent:
- target host must still resolve to a public IP address;
- when `AWF_CALLBACKS_REQUIRE_HTTPS=true`, `https://` is required;
- when `AWF_CALLBACKS_ALLOWED_HOSTS` is set, the callback host must be in the
  allowlist.

Malformed targets and resolved private or non-public delivery addresses are
recorded as delivery failures with `error_code = CALLBACK_TARGET_INVALID`.
Configurable HTTPS and allowlist policy violations are recorded with
`error_code = CALLBACK_TARGET_POLICY_VIOLATION`. Both paths are retried according
to normal retry settings; they are never sent as successful callbacks.
Target validation timeouts use
`error_code = CALLBACK_TARGET_VALIDATION_TIMEOUT` so operators can distinguish
transient validation latency from permanently invalid callback targets; retry
settings are unchanged.
When validation succeeds but consumes the full delivery timeout before the POST
can start, delivery records use `error_code = CALLBACK_DELIVERY_BUDGET_EXCEEDED`.

---

## Locks and Overlap

### List owned-path locks

```bash
curl "http://localhost:8000/v1/locks?limit=50"
```

Filter by repo, task class, or workspace status:

```bash
curl "http://localhost:8000/v1/locks?repo_url=git@github.com:example/app.git&task_class=refactor_task"
```

### Overlap graph

Visualize advisory path overlap between active workspaces.

```bash
curl "http://localhost:8000/v1/locks/overlap-graph?limit=100"
```

---

## Runtime

Inspect a workspace's Docker Compose stack and runtime health.

Auth required.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/runtime"
```

---

## Metrics

### Workspace reliability summary

```bash
curl "http://localhost:8000/v1/metrics/workspaces/summary?since_hours=168"
```

### Failure analysis summary

```bash
curl "http://localhost:8000/v1/metrics/failures/summary?since_hours=24&limit=10"
```

### Resource saturation

```bash
curl "http://localhost:8000/v1/metrics/resources/saturation"
```

### SLO metrics

```bash
curl "http://localhost:8000/v1/metrics/slo?since_hours=168"
```

---

## Secret Leases

See [Secret lease status (operator metadata)](#secret-lease-status-operator-metadata)
for the canonical `/v1/workspaces/{id}/secret-leases` endpoint documentation.

---

## PR Monitor Adoption

Adopt an already-open GitHub PR into AWF monitoring without rerunning the
original coding agent. Auth required. AWF derives deterministic repo/PR idempotency from
the normalized repository identity and PR number; adoption does not require a
caller-provided idempotency key.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/adopt-pr" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_slug": "example/app",
    "pr_number": 42,
    "auto_merge": true,
    "initial_review_grace_period_seconds": 900,
    "reason": "attach AWF to existing PR"
  }'
```

The response is `PullRequestMonitorAdoptionResponse` and includes the adopted
`workspace_id`, `monitor_policy`, `validation_provenance`, `status_url`,
`events_url`, `logs_url`, and `attached_existing`. A repeat adoption for the
same repo/PR and same monitor policy returns the same workspace with
`attached_existing=true`; policy changes return `PR_ADOPTION_POLICY_CONFLICT`.
Agent `model` and `effort` overrides are part of that raw monitor policy:
omitting them requests the default/no-override policy and conflicts with an
existing live adoption pinned to explicit `agent_model` or `agent_effort`
values.
If the previous adoption row is terminal or superseded, a retry creates a fresh
monitor workspace with `attached_existing=false` and records the previous
terminal adoption lineage.
Closed or merged PRs return structured errors such as `PR_ALREADY_CLOSED` or
`PR_ALREADY_MERGED`.

Inspect the adopted monitor:

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/events?limit=50"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/operations?limit=25"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/logs"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/validation"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/merge-queue"
```

Recovery operations:

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/remonitor" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: remonitor-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "worker restarted"}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/refresh" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: refresh-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "refresh GitHub state"}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/validate" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: validate-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "prove current head", "requested_tier": 2}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/rebase" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: rebase-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "target branch advanced"}'
```

See [PR Monitor Adoption](PR_MONITOR_ADOPTION.md) for the CLI/MCP equivalents,
GitHub auth readiness, `auto_merge=true` versus manual monitor policy,
terminal adoption retry behavior, console inspection, and mocked-local demo
path.

---

## OpenAPI Spec

The full OpenAPI specification is available at:

- Live: `GET /openapi.json` on the running service
- Stable artifact: `openapi.json` in the repository root
- Interactive docs: `/docs` (Swagger UI) on the running service

To regenerate the checked-in artifact:

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py
```

To verify the checked-in artifact has not drifted:

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```
