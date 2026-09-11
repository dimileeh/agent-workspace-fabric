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
`remonitor`, `refresh`, `revalidate`, `cancel`, `retry`

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
UI renders `—` and never coerces null to `0`. When the optional
`count_evidence` object is present and valid, the UI may render a confirmed
lower bound for a null exact count, but it visibly qualifies the value as
`N confirmed`; it never replaces the null exact value or presents the lower
bound as an exact total. A non-null exact count always takes precedence.

### Optional count evidence

`count_evidence` is an optional, nullable schema-version-1 extension. When
non-null it has four required fields and permits no unknown fields:

| Field | Contract |
| --- | --- |
| `total_workspaces` | Strict nonnegative integer population in the authorized project-wide snapshot |
| `status_known_workspaces` | Strict nonnegative integer count with authoritative workflow status |
| `status_unknown_workspaces` | Strict nonnegative integer count without authoritative workflow status |
| `confirmed_counts` | Object containing the same ten required names as `counts`, each a strict nonnegative integer lower bound |

All evidence comes from the same authorized project-wide snapshot as the
summary. The following invariants are required:

- `status_known_workspaces + status_unknown_workspaces = total_workspaces`.
- Every confirmed counter is at most `status_known_workspaces`.
- The documented subset, overlap, and disjoint-active constraints apply to
  `confirmed_counts` exactly as they apply to non-null exact counts.
- Every non-null exact counter equals its confirmed counterpart when evidence
  is present. Null exact counters remain null.
- `coverage.status=complete` cannot coexist with a nonzero unknown-status
  population.
- Missing, invalid, or stale current-attempt status is status-unknown and
  contributes to no confirmed counter.
- A known authoritative workflow status can still lack attention evidence or
  the authoritative original terminal timestamp. Those counters remain
  incomplete, and `coverage.status` plus notes continue to name the attention
  or terminal-window gap independently of status coverage.
- Terminal-window confirmation requires the authoritative original terminal
  timestamp. A later observation, collection, or unrelated update timestamp
  must not be substituted.
- `status_unknown_workspaces=0` does not by itself upgrade overall coverage to
  `complete`; attention, terminal-time, and other required evidence may still
  be partial.

For example, a snapshot of 29 workspaces with 24 known statuses and five
unknown statuses may publish all ten confirmed counters as zero. The UI shows
`0 confirmed` for null exact counters and reports
`24 of 29 workflow statuses known; 5 unknown`. If the next same-scope snapshot
adds one authoritative running workflow, the evidence is total 30, known 25,
unknown five, with `active=1` and `executing=1` in `confirmed_counts`; those
null exact counters render as `1 confirmed`.

Omission and explicit null are accepted for reader compatibility. Core's local
exact-summary producer omits the field when unused, so its existing serialized
payload shape does not gain a null key. Shared readers that predate this
extension continue to receive the old shape; compatible shared readers must be
deployed before a hosted producer begins emitting evidence.

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
- `last_success_at` — last fully successful summary build (client may retain prior value across outages). Required key; `null` only until a successful snapshot exists (first partial/unknown build). `coverage.status=complete` must carry a timestamp — never fabricate one.

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
| `confirmed_execution_model` / `confirmed_execution_model_source` | Only when execution evidence confirms; **never** label `task_policy` / `default` / `auto` (or other non-confirming labels such as `inferred` / `configured`) as confirmed. Any other nonempty source is valid confirmation provenance (not a closed allowlist). |
| `started_at` / `finished_at` / `duration_seconds` | Workflow timing when recorded |
| Native vs workflow finish | Missing history = explicitly not recorded |

Existing Core fields (`agent_model`, `agent_model_source`, …) remain compatible.
Helpers prefer explicit requested/confirmed when present.

## Local vs hosted examples

See:
- [`console/fixtures/v1/capabilities.local.json`](./console/fixtures/v1/capabilities.local.json)
- [`console/fixtures/v1/capabilities.hosted.json`](./console/fixtures/v1/capabilities.hosted.json)
- [`console/fixtures/v1/capabilities.identity-matrix.json`](./console/fixtures/v1/capabilities.identity-matrix.json)
- [`console/fixtures/v1/capabilities.route-matrix.json`](./console/fixtures/v1/capabilities.route-matrix.json)
- [`console/fixtures/v1/dashboard-summary.local.json`](./console/fixtures/v1/dashboard-summary.local.json)
- [`console/fixtures/v1/dashboard-summary.hosted.json`](./console/fixtures/v1/dashboard-summary.hosted.json)
- [`console/fixtures/v1/cloud-runtime.hosted.json`](./console/fixtures/v1/cloud-runtime.hosted.json)
- [`console/fixtures/v1/dashboard-summary.partial.json`](./console/fixtures/v1/dashboard-summary.partial.json)
- [`console/fixtures/v1/dashboard-summary.no-prior-success.json`](./console/fixtures/v1/dashboard-summary.no-prior-success.json)
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
- [ ] Include `identity` with non-empty `backend_id`, `scope`, and `tenant_id` on every
      capabilities response (required for hosted; used to advance console feed epochs).
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
`identity` (`backend_id`, `scope`, `tenant_id`) — optional for `backend_kind=local`.

### Hosted identity (required when `backend_kind=hosted`)
`identity` with non-empty `backend_id`, `scope`, and `tenant_id`. Hosted providers
must supply a stable tenant discriminator so console clients can advance authorized
feed epochs on tenant/context switches; omitting identity collapses every tenant onto
the same key and allows cross-tenant in-flight responses to leak into the UI.
Console clients reject incomplete hosted identity at parse time and never use the
collapsing `hosted|||` epoch key; losing a prior identity key also clears authorized
feeds.

### Widget/diagnostic entry
- Available: `id` (must be in that collection's route inventory),
  `availability=available`, `route` (exact inventory relative `/v1/...` for that
  id — see `console/fixtures/v1/capabilities.route-matrix.json`; may include
  `{workspace_id}`), `semantics`. Unknown inventory id, or missing/null/
  non-inventory route (e.g. `/v1/wrong-route`) ⇒ malformed (fail closed).
  Widgets without an inventory route (`telemetry`, `allocation`, `cost`) cannot
  be `available`.
- Unsupported: `id`, `availability=unsupported`, `reason_code`, `message` (`route` omitted)
- Controls: `id`, `availability`, `semantics` required; available controls omit route.

### Dashboard summary (required)
`schema_version`, `scope`, `generated_at`, `as_of`, `last_success_at`, `window`, `coverage`, `counts`, `overlap`

Optional: `count_evidence` (object or `null`, as defined above).

`last_success_at` must be present. The value is an RFC 3339 timestamp or `null`
when no fully successful summary exists yet. `coverage.status=complete` requires
a timestamp.

### Counts
Each count key is required on the object; values are strict nonnegative
`integer | null`. The ten exact fields and their meanings are unchanged by the
optional evidence extension.
