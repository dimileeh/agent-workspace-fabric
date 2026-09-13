# Shared Console Stage 3 telemetry consumer contract

The generic reader, projection, and reusable component now serve the selected
workspace inspector through capability-gated reads. This does not activate
production or establish production acceptance. The component harness remains behind the existing
`AWF_CONSOLE_TEST_HARNESS` entry.

## Historical producer evidence from reader convergence

The earlier reader-convergence implementation inspected these exact files using authenticated `gh api`
reads from `dimileeh/awf-cloud` at
`70567f1e152d01b2220244481e23b1fab5422284` (PR640):

- `apps/api/awf_cloud_api/console_telemetry/types.py`
- `apps/api/awf_cloud_api/console_telemetry/query.py`
- `apps/api/awf_cloud_api/console_telemetry/metrics_normalize.py`
- `apps/api/awf_cloud_api/console_telemetry/fixtures.py`
- `tests/test_console_telemetry_persistence_postgres.py`

Read command, substituting one of the paths above:

```sh
gh api 'repos/dimileeh/awf-cloud/contents/<path>?ref=70567f1e152d01b2220244481e23b1fab5422284' --jq '.content' | base64 -d
```

That producer permits null admitted/estimate and compute class, defines
`unpriced`, emits memory gauges with null starts, and derives freshness from
the oldest latest family/container timestamp. Its query uses its own clock
for sample selection and reads retained cost independently of the selector.

The pinned version does **not** emit `window_end_at` or `estimate_scope`, or
bound each query family to 2048 samples. These are the approved paired changes,
not claims about the pinned version. The supplied independent audit reports
four canonical fixtures passing but only one of nine PostgreSQL query cases
passing the prior Core consumer at `8590c28946389f1867b192dffbfaac14c682e620`
(PR978). This patch has not rerun that audit.

## Reader and display semantics

| Contract | Consumer behavior |
| --- | --- |
| `admitted: null` | Not recorded. Usage and retained estimate can remain present; allocated projection is partial. |
| `estimate: null` | Not recorded, with null dollars, durations and provenance. No synthetic estimate object in the parsed model. |
| Recorded `unpriced` | Distinct from not recorded and unallocated; null USD and zero priced duration are required. Recorded unpriced duration is retained, including zero. |
| Explicit unallocated | Requires matching estimate state, null USD, zero durations, no admission and no samples. Shared Core monitor remains unallocated, never free. |
| Null or unknown bounded compute class | Unknown class and partial admission; recorded resources remain unchanged. No fallback pricing identity. |
| Memory gauge | Null start is accepted only with interval end equal to sample time. Internal start becomes that instant, preserving complete container partition aggregation. Wire evidence is unchanged. |
| CPU rate | Finite ordered start/end and sample within measurement required. Start may precede chart left edge. Legacy equal CPU endpoints remain valid. |
| `window_end_at` | Optional bounded RFC3339 string. Absence uses `observed_at`; explicit null, undefined, wrong types or malformed strings fail closed. |
| Chart bounds | Sample time and interval end must be in inclusive `[end - view, end]`. Legacy non-null memory starts retain their bounds guard. Sub-millisecond and offset comparisons remain exact. |
| `observed_at` | Independently validated and nullable; cannot exceed chart end but can predate its left edge. It remains freshness evidence, never replaced with fetch/query time. |
| Future query clock | Marks projection future/stale even when meter timestamps are past; historical mode also retains this guard. |
| `estimate_scope` | Optional `view` or `resource_attempt`; absence means `view`. Explicit null/unknown/wrong types fail closed. |
| View estimate | Combined priced/unpriced duration cannot exceed selected view. |
| Resource-attempt estimate | Retained UID/placement-attempt lifetime, independent of selector. Each integer duration retains its 1e15 bound; sum is at most 2e15, finite and safe in JavaScript. Dollars are never recomputed. |
| Component copy | “Allocation estimate,” with scope explanation. Missing durations read “Not recorded.” No whole-workspace/all-attempt total claim. |

Malformed non-null admission, estimates, currencies, scalar values, timestamps,
provenance and UID contradictions still fail closed. Missing fields inside a
non-null admission or estimate are not silently treated as valid records.
Projection excludes ownership, resource UIDs, job evidence and machine notes.
The consumer is not a tenant authorizer and cannot establish query cache or
placement-attempt isolation from a single presentation.

The 2048 samples/family cap, bounded strings, safe byte precision, duplicate
container rejection, full-interval CPU partition comparisons, and same-instant
aggregation remain in place. No consumer truncation or summing different
instants was introduced. Cloud must supply bounded whole-container partitions
over the full selected range with first/latest points and truthful gaps/quality.
A producer returning 2049 or 2880 points still fails closed.

## Regression evidence and synchronization

All four original synthetic producer JSON files and their recorded hashes are
unchanged; see [fixture provenance](../apps/console/lib/fixtures/console-workspace-telemetry/PROVENANCE.md).
New `paired` unit/harness cases are explicitly labeled **consumer mutations** of
that evidence. They are not new producer DTO exports or database integration tests.

| Scenario | Local evidence | Actual normalizer → isolated PostgreSQL → query → to_contract_dict |
| --- | --- | --- |
| Cold absent, checkpoint/no samples, active/no estimate | Parser/projection mutations; cold browser display | PR643 exports imported |
| Memory gauge, two aligned containers, CPU 60s lag | Parser/projection mutations; mixed-clock browser display | PR643 exports imported |
| One-hour/two-hour terminal under 1h, retained cleaned terminal | Scope/duration mutations; retained historical browser display across selectors | PR643 exports imported |
| 24h two-container full-range partitions, missing family/incomplete partition | At-cap acceptance, 2049/2880 rejection, truthful missing totals | Bounded PR643 exports imported |
| Stale family/fresh query clock, malformed input and precision | Existing and new negative/unit/browser cases | PR643 exports imported; later independent release audit remains |
| Shared unallocated | Unchanged producer fixture and browser regression | PR643 query export imported |
| Cross-resource and evidence UID conflicts | Existing and new fail-closed consumer tests | Tenant/resource/attempt database/cache isolation remains Cloud-owned |

At the earlier reader-convergence stage, counterpart query exports were not
supplied. Stage 3 below now synchronizes all 13 exact PR643 exports and their
producer provenance, including scenario clocks and hashes. Cloud remains
unmounted: the actual PostgreSQL chain was not rerun in Core or replaced with a
synthetic Core database. Independent paired release/active-run acceptance remains
required.

Changed legacy assertions are limited to: null estimate/admission no longer
malformed (non-null negative controls retained); CPU starts may precede chart
bounds; unknown compute class stays unidentified while region stays verbatim;
and allocation-estimate wording. Legacy memory interval, view-duration,
currency, identity, numeric and malformed-field tests remain.

## Historical reader-convergence validation

From `apps/console`:

```sh
node --test --test-concurrency=4 --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/console-workspace-telemetry.test.mjs
npx eslint lib/console-workspace-telemetry.ts lib/console-workspace-telemetry.test.mjs components/console-workspace-telemetry.tsx tests/harness/console-workspace-telemetry-harness.tsx tests/console-workspace-telemetry.spec.ts
npx playwright test tests/console-workspace-telemetry.spec.ts --workers=2
```

The eight initial paired unit groups failed before implementation. Final focused
unit run: 126 passed. Browser regression reproduced “Unpriced” for an absent
estimate before the component change; final telemetry browser file: 18 passed.
Focused lint has no errors (one existing unused-variable warning). A TypeScript
compiler program rooted only at the changed telemetry TypeScript files and using
existing options plus Node ambient types reports zero diagnostics.
Full validation, coverage/provenance and merge gating belong to AWF/GitHub after
agent completion and were not executed here.

## Stage 3 selected-workspace integration

Schema v1 advertises the same exact `/v1/workspaces/{workspace_id}/telemetry`
route for `telemetry`, `allocation`, and `cost`. The inspector performs one
cohesive read for the selected workspace and view (`1h` default, `6h`, `24h`),
with no list fanout or history preload. Periodic reads start at least 60 seconds
after the previous request settles; requests time out after 30 seconds. Ordinary
parent refreshes do not restart telemetry polling. Unsupported sections are omitted.

The shared URL helper uses `/api/awf` locally or configured `/api/core-console`
in hosted mode and carries the configured authorized `org_id`/`project_id`.
The ID is the existing Cloud workspace-record ID. Namespace, cell, resource UID,
attempt and upstream URL are not user routing inputs. The response is the direct
`TelemetryPresentation.to_contract_dict()` object, without an envelope.

The child owns cancellation, loading, malformed/error state and same-identity
last-success display. Workspace, view, backend/tenant context, authorization epoch
and gate changes invalidate retained data and in-flight work. A telemetry endpoint
401/403 is feed-local: it clears retained telemetry and stops that reader while
workspace cards and the inspector remain available. Supplied ownership is checked
against request context;
observed resource/attempt replacement discards prior retained data. Browser reads
use `cache: no-store`. Provider sample age advances independently of request success;
a longer view does not turn live data into historical data. Cost remains the
resource-attempt estimate, independently of chart selection and LLM cost.

A pending same-context capability refresh or transient capability-endpoint outage
retains the last valid negotiation; an outage does not withdraw available telemetry.
Malformed/missing capability contracts and explicit withdrawal cancel selected reads
and clear their retained metrics. Backend replacement and authorization denial also
invalidate prior-context work. Recovery after invalidation starts a fresh authorized read;
late successes from invalidated requests cannot restore the old metrics.

`tests/dashboard-telemetry.spec.ts` exercises these transitions with a telemetry
poll held after an initial success, including 401/403 recovery. It also checks
unchanged, timestamp-only and unrelated capability refreshes against the minute
cadence, plus delayed reads and the dense 24h export with a paginated list. The
interaction checks retain the existing 1,000ms pane threshold, assert stable user
scroll and selection through dense rendering, and allow only one requested history
page after scrolling to the end. These are synthetic browser checks, not hosted
production acceptance.

For an unpriced estimate with no amount and zero priced seconds, absent or null
`estimate.evidence.source` means unknown. Malformed non-null provenance, conflicting
rate versions/resource attribution and priced contradictions remain rejected.

Thirteen synthetic persisted query exports plus their unchanged producer provenance
were imported from Cloud PR643 commit `8d2b59ead8ca3f2dcb34e6a049d40d48a2fd7c21`.
Their hashes and import paths are recorded in
`apps/console/lib/fixtures/console-workspace-telemetry/PROVENANCE.md`. Core's audited
base was `2cc77e2c235560aabcfcb28f08cb22ba814b858d` (PR981). These real parser/projector
fixture tests supplement the user-reported original 13-case database-to-Core audit;
this integration did not rerun that database audit or copy private Cloud source.

Local capabilities stay unsupported and make no GCP calls. Hosted support must be
deployed dark with explicit default-off widget/collector configuration before a
later audited activation. Available widgets are not withdrawn because of live
provider health. This is not production acceptance: the later cross-repository
release/active-run audit must verify the Cloud BFF mapping to
`/v1/orgs/{org_id}/projects/{project_id}/core-console/workspaces/{workspace_id}/telemetry`,
authorization before every cache read/return, a maximum 60-second query cache keyed
by full tenant/workspace/resource UID/placement attempt/view identity, and browser
response `Cache-Control: no-store`. The pinned producer's cache helper default is
300 seconds; the endpoint integration must explicitly satisfy the shared 60-second
limit. AWF/GitHub own broad validation, coverage and merge gating after completion.

Focused integration validation: 200 Node tests, 32 Python capability tests, and
173 distinct browser cases passed across the six planned regression files and
the final 17-case telemetry rerun. Focused lint/type and OpenAPI drift checks
passed (existing lint warnings only). Broad AWF/GitHub gates remain external.
