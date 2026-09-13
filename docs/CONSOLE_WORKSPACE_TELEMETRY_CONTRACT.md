# Shared Console Stage 3 telemetry consumer contract

This change converges the generic reader, projection, and reusable component.
It does not activate telemetry, add polling or routes, or establish integration
or production acceptance. The harness remains behind the existing
`AWF_CONSOLE_TEST_HARNESS` entry.

## Producer evidence and paired additions

The implementation inspected these exact files using authenticated `gh api`
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

## Regression evidence and remaining synchronization

All four original synthetic producer JSON files and their recorded hashes are
unchanged; see [fixture provenance](../apps/console/lib/fixtures/console-workspace-telemetry/PROVENANCE.md).
New `paired` unit/harness cases are explicitly labeled **consumer mutations** of
that evidence. They are not new producer DTO exports or database integration tests.

| Scenario | Local evidence | Actual normalizer → isolated PostgreSQL → query → to_contract_dict |
| --- | --- | --- |
| Cold absent, checkpoint/no samples, active/no estimate | Parser/projection mutations; cold browser display | Pending paired exports |
| Memory gauge, two aligned containers, CPU 60s lag | Parser/projection mutations; mixed-clock browser display | Pending paired exports |
| One-hour/two-hour terminal under 1h, retained cleaned terminal | Scope/duration mutations; retained historical browser display across selectors | Pending paired exports |
| 24h two-container full-range partitions, missing family/incomplete partition | At-cap acceptance, 2049/2880 rejection, truthful missing totals | Pending bounded producer exports |
| Stale family/fresh query clock, malformed input and precision | Existing and new negative/unit/browser cases | Pending paired exports and independent audit |
| Shared unallocated | Unchanged producer fixture and browser regression | Pending query export |
| Cross-resource and evidence UID conflicts | Existing and new fail-closed consumer tests | Tenant/resource/attempt database/cache isolation remains Cloud-owned |

Cloud is not mounted here and counterpart query exports were not supplied.
The actual PostgreSQL chain was not executed in Core or replaced with a
synthetic Core database. Final fixture synchronization requires producer commit,
generating test/command, scenario, fixed query clock and SHA-256 for each export,
followed by an independent paired audit. Preserve source bytes and verify the
consumer's internal gauge normalization against those bytes. This is the
remaining integration dependency.

Changed legacy assertions are limited to: null estimate/admission no longer
malformed (non-null negative controls retained); CPU starts may precede chart
bounds; unknown compute class stays unidentified while region stays verbatim;
and allocation-estimate wording. Legacy memory interval, view-duration,
currency, identity, numeric and malformed-field tests remain.

## Focused validation

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
