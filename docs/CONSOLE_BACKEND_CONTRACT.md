# Console Backend Contract v1

Versioned, backend-neutral contract for the shared AWF Console.
`schema_version` is a top-level integer. Clients that see a value other than `1`
**fail closed** for controls and show an explicit capability/contract error.

Canonical fixtures live in [`docs/console/fixtures/v1/`](./console/fixtures/v1/)
and are consumed by Python validators, TypeScript/browser tests, and
(unchanged) by a subsequent Cloud provider.

## Routes

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/console/capabilities` | Bearer `AWF_API_TOKEN` | Advertise backend kind + widget/diagnostic/control availability |
| `GET` | `/v1/console/dashboard-summary` | Bearer `AWF_API_TOKEN` | Authoritative fleet counters independent of capacity probes |
| `GET` | `/v1/console/cloud-runtime` | Bearer (hosted only) | Hosted queue/provisioning/admission evidence (Core marks unsupported) |

Known relative `/v1/...` routes only. **No absolute external URLs** in capability data.

Capability advertisement is **not** live health. Outages keep an error plus last-
successful snapshot/time; they must not be rewritten as `unsupported` or coerced to zero.

## Enums

### `backend_kind`
- `local` — Core / single-node scope
- `hosted` — Cloud / tenant scope

### `availability`
- `available` — client may fetch the advertised relative `route` (when present)
- `unsupported` — omit widget / disable control; show bounded `reason_code` + `message`

### Widget ids
`fleet_summary`, `resource_capacity`, `cloud_runtime`, `telemetry`, `allocation`, `cost`

### Diagnostic ids
`reliability`, `merge_queue`, `failures`, `workspace_runtime`, `workspace_events`,
`workspace_operations`, `workspace_logs`, `workspace_stream`

Optional workspace detail/stream diagnostics gate subrequests after the basic
workspace detail (`GET /v1/workspaces/{id}`) navigation request. Templated
routes use `{workspace_id}`.

### Control ids
`remonitor`, `refresh`, `revalidate`, `cancel`

### Coverage status
`complete` | `partial` | `unknown`

### Unsupported reason codes (bounded)
`backend_kind_local`, `backend_kind_hosted`, `not_implemented`, `policy_disabled`

## Failure distinctions (fail closed for controls)

| Condition | Client behavior |
| --- | --- |
| Missing capabilities response / 404 | Legacy-safe workspace navigation + explicit capability error; **do not** infer local privileges or poll unsupported feeds |
| `schema_version != 1` | Explicit contract error; controls disabled; no privileged polls |
| `401` / `403` | Clear stale authorized data; disable controls |
| Malformed payload | Treat as capability failure (same as missing) |
| Feed outage after prior success | Keep error + last-successful data/`last_success_at`; do **not** show zero or “unsupported” |

Never guess mode from hostname, browser location, query strings, or failed metrics calls.

## Dashboard summary semantics

### Counts
| Field | Meaning (local Core) |
| --- | --- |
| `active` | Non-terminal workspaces |
| `executing` | `running` + `validating` + `pushing` |
| `monitoring_pr` | Status `monitoring_pr` |
| `awaiting_operator` | Status `blocked` |
| `awaiting_human` | `monitoring_pr` ∧ `awaiting_human_since IS NOT NULL` |
| `retrying` | Status `recovering` |
| `queued` | Persisted queue evidence (`requested` local semantics) |
| `*_last_window` | Terminal statuses in the rolling window by `updated_at` |

**Null ≠ zero.** Incomplete fields stay `null` with `coverage.status=partial|unknown`.
UI renders `—` and never coerces null to `0`.

A stopped **native** execution is **not** a completed overall workflow. Native
runtime finish and workflow finish remain distinct presentation concepts.

### Overlap (documented, not mutually exclusive buckets)
- `awaiting_human` ⊆ `monitoring_pr` (flag, not a separate status)
- `awaiting_operator` (`blocked`) ∈ `active` but ∉ `executing`
- `retrying` (`recovering`) ∈ `active` but ∉ `executing`

### Window
- `window.anchor` = `generated_at`
- `window.since_hours` default `24`
- `window.start` = `generated_at - since_hours`
- Terminal window uses persisted `updated_at`
- Deleted/destroyed rows follow normal DB retention; summary does not invent history

### Timestamps
- `generated_at` — response build time
- `as_of` — data freshness bound (may equal `generated_at`)
- `last_success_at` — last fully successful summary build (client may retain prior value across outages)

### Scope
- Core: `scope=local` means the whole authorized **control-plane fleet** for
  this Core instance (all workspaces in the control-plane DB), **not** the
  Docker/capacity worker node filter. Current and window counters must agree
  on that fleet scope. No Docker probes for counts.
- Cloud: `scope=tenant` against the **same** schema (implemented in awf-cloud later)

## Additive workspace presentation fields

Optional on console overview/detail types (additive; public lifecycle/MCP handles unchanged):

| Field | Notes |
| --- | --- |
| `requested_model` / `requested_effort` | Requested identity |
| `requested_model_source` / `requested_effort_source` | Provenance of request |
| `confirmed_execution_model` / `confirmed_execution_model_source` | Only when execution evidence confirms; **never** label `task_policy` / `default` / `auto` as confirmed |
| `started_at` / `finished_at` / `duration_seconds` | Workflow timing when recorded |
| Native vs workflow finish | Missing history = explicitly not recorded |

Existing Core fields (`agent_model`, `agent_model_source`, …) remain compatible.
Helpers prefer explicit requested/confirmed when present.

## Local vs hosted examples

See:
- [`console/fixtures/v1/capabilities.local.json`](./console/fixtures/v1/capabilities.local.json)
- [`console/fixtures/v1/capabilities.hosted.json`](./console/fixtures/v1/capabilities.hosted.json)
- [`console/fixtures/v1/dashboard-summary.local.json`](./console/fixtures/v1/dashboard-summary.local.json)
- [`console/fixtures/v1/dashboard-summary.hosted.json`](./console/fixtures/v1/dashboard-summary.hosted.json)
- [`console/fixtures/v1/cloud-runtime.hosted.json`](./console/fixtures/v1/cloud-runtime.hosted.json)
- [`console/fixtures/v1/dashboard-summary.partial.json`](./console/fixtures/v1/dashboard-summary.partial.json)
- [`console/fixtures/v1/workspace-presentation.sample.json`](./console/fixtures/v1/workspace-presentation.sample.json)

### Hosted Cloud Runtime widget
When `cloud_runtime` is `available`, clients fetch the relative route and render
queue age, provisioning, and admission/quota evidence only. Telemetry /
allocation / cost stay `unsupported` until a later backend implements them.
**No fake charts, bills, or free shared monitor runtime.**

## Route inventory / OpenAPI compatibility

- Core OpenAPI (`openapi.json`) exports `/v1/console/capabilities` and
  `/v1/console/dashboard-summary`.
- Cloud must implement identical paths + `schema_version=1` payloads.
- Console BFF catch-all forwards `/api/awf/console/...` → `/v1/console/...`.
- Fixtures under `docs/console/fixtures/v1/` are the golden contract for Cloud.

## Rollout / rollback

1. **Deploy Core** with `/v1/console/*` **before** any hosted console artifact that requires them.
2. Shared UI: if capabilities missing/404 → legacy-safe workspace navigation + explicit capability error; no inferred local feeds.
3. Rollback UI independently of Core routes (routes remain harmless read-only).
4. Cloud implements the same contract in awf-cloud **after** this Core contract is audited; then Cloud may advertise hosted capabilities.
5. Older Cloud backends must **not** receive the upgraded shared UI until Cloud routes exist.
6. This Core change does **not** bump awf-cloud pins or deploy Cloud.

## Cloud implementer checklist

- [ ] Implement identical paths + `schema_version=1` payloads (tenant-wide summary scope).
- [ ] Advertise `backend_kind=hosted`; mark local Docker capacity unsupported.
- [ ] Supply Cloud Runtime evidence fields per `cloud-runtime.hosted.json`; leave cost/telemetry unsupported until real collectors exist.
- [ ] Pass Core fixture validators unchanged (or publish golden copies from `docs/console/fixtures/v1`).
- [ ] Do not put absolute external URLs in capabilities.
- [ ] Preserve Core-compatible workspace overview fields; additive presentation only.
- [ ] Auth denial and outage semantics match Core (fail closed; stale last-success).
- [ ] No cross-context response/cache reuse across tenants; Core has no tenant storage.

## Required vs optional fields

### Capabilities (required)
`schema_version`, `backend_kind`, `generated_at`, `widgets`, `diagnostics`, `controls`

### Capabilities (optional)
`identity` (`backend_id`, `scope`, `tenant_id`)

### Widget/diagnostic entry
- Available: `id`, `availability=available`, `route` (relative `/v1/...`, may
  include `{workspace_id}`), `semantics`. Missing route ⇒ malformed (fail closed).
- Unsupported: `id`, `availability=unsupported`, `reason_code`, `message` (`route` omitted)
- Controls: `id`, `availability`, `semantics` required; available controls omit route.

### Dashboard summary (required)
`schema_version`, `scope`, `generated_at`, `as_of`, `last_success_at`, `window`, `coverage`, `counts`, `overlap`

### Counts
Each count key is required on the object; values may be `number | null`.
