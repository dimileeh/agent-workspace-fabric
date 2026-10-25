# Protected node identity and material receiver (foundation)

**Activation is CLOSED. This is not complete VM I3 and does not enable VM work.**
Core implements a default-disabled identity fence, private file receiver and
unwired protected lease resolver. Cloud retains Organisation/Project
authorization, broker access, placement and billing. Namespace strings are
opaque scoping labels, never tenant authorization or authority to change owners.

## Deployment and identity

Normal local Core is unchanged. No hosted configuration means all
`/v1/hosted-node/*` routes return 404. Enabling requires trusted server
`Settings.hosted_node` (or JSON `AWF_HOSTED_NODE`) with these fields:

| Field | Contract |
| --- | --- |
| `owner` | Stable opaque logical owner |
| `incarnation` | Physical incarnation ID |
| `recovery_generation` | Positive monotonic signed int64 |
| `controller_token` | Separate private controller bearer, at least 16 characters |
| `material_root` | Existing absolute private directory on Linux tmpfs, mode 0700, owned by the Core effective UID |

Do not put real credentials in checked-in configuration, profiles, task text or
command arguments. Supply settings through the trusted deployment's secret
configuration. Root and all ancestors are opened without following symlinks.
Use an owner-specific root outside checkout, logs and artifacts. tmpfs must have
a deployment-imposed size limit and an appropriate swap policy; tmpfs alone does
not guarantee RAM pages cannot be swapped. Do not expose this directory to
untrusted host processes or allow workloads to replace mount sources.

The receiver must be reachable **only** over an externally secured private
encrypted control transport (for example a controller-authenticated TLS proxy).
Core does not implement mTLS, trust forwarded headers as evidence of encryption,
fetch caller URLs, or infer tenant authorization. The deployer owns network
isolation, transport security and controller credential distribution. This
foundation must not be deployed alongside a worker, MCP executor, or another
execution ingress. Those ingress paths are not integrated with this receiver.

Startup registers the server-configured tuple in PostgreSQL and advances a
durable process boot epoch. A lower recovery generation or different incarnation
at the same generation is rejected. A newer boot invalidates the old process,
even on the same VM. Every protected operation checks current durable authority.
Database uncertainty fails closed. A database restore requires external
coordination; monotonicity assumes PostgreSQL history has not been rolled back.

Identity is **not proof an old VM stopped**. Epoch acknowledgement does not resume
workers or establish external fencing. Those remain I5 responsibilities.

## Authentication, HTTP and WebSocket fencing

Existing APIs retain their existing operator bearer. Dedicated mutations require
the distinct controller bearer. An operator bearer can read identity but cannot
deliver material. Missing/wrong credentials return 401; an operator attempting a
controller operation returns 403. Equal controller and operator tokens fail closed with 503
`CONTROLLER_TOKEN_MUST_DIFFER`.

When enabled, all HTTP reads/controls and WebSocket handshakes require:

- `X-AWF-Expected-Owner`
- `X-AWF-Expected-Incarnation`
- `X-AWF-Expected-Recovery-Generation`
- `X-AWF-Expected-Boot-Epoch`

All four must exactly match the server tuple, including canonical decimal
generation/epoch strings. They are expectations, never current identity inputs.
Missing, foreign, stale or superseded identity is denied before request parsing
and endpoint effects. WebSocket acceptance and every protected outbound frame
recheck durable identity; denial closes with policy code 1008.

The only exceptions are `GET /healthz` (minimal liveness) and authenticated
`GET /v1/hosted-node/identity` (bootstrap, still verifies durable authority).
Readiness, docs, logs, artifacts, callbacks and all other routes are fenced.

Existing mutations are closed by default, including create, adopt, retry,
remonitor, guide, refresh, validate, rebase, callbacks and queue changes.
Only exact workspace stop/cancel and destroy paths are allowed through to their
existing authenticated handlers. Denied execution returns 403
`HOSTED_RUNTIME_INTEGRATION_REQUIRED`. There is no setting to reopen admission.

## Canonical HTTP schemas

All paths below have prefix `/v1/hosted-node`. OpenAPI is generated from the real
application in `openapi.json`. Request objects forbid extra fields. Identifiers
use 1–128 ASCII characters from `[A-Za-z0-9_-]`. Revisions, authorization
generations and fences are positive signed int64 integers, not strings or booleans.

`Identity` is `{owner, incarnation, recovery_generation, boot_epoch}`.

`Preparation` is:
`{namespace, workspace_id, preparation_id, purpose, target, expires_at}`.

- Initial supported purpose is exactly `credential`.
- Target is a canonical descendant of `/run/awf/secrets/`, maximum 256
  characters, using ASCII letters/digits and `/_.-`. Traversal, empty path
  segments, trailing slash and noncanonical paths are rejected.
- Expiry is timezone-aware RFC3339, at most one hour ahead on preparation/install.
  Material expiry cannot exceed its preparation expiry.
- A preparation's identity and target are immutable within an owner and bound to its boot.
  Only the explicit bounded renewal below can extend its expiry. It can
  precede a Workspace row and does not create or admit execution. A new boot
  requires a new preparation ID.

`Binding` contains all Preparation fields plus:
`{identity, idempotency_key, grant_id, revision, authorization_generation, fence}`.

| Method/path | Body | Response |
| --- | --- | --- |
| GET /identity | None | Identity plus `acknowledged: boolean, activation: "closed"` |
| POST /identity/acknowledge | `{idempotency_key}` | Identity response; same-key replay is safe, another key conflicts within the boot |
| POST /preparations | Preparation | `{"status":"prepared"}`; exact replay is safe |
| POST /preparations/renew | PreparationRenewal | `{"status":"renewed", "receipt_id":..., "preparation_id":..., "expires_at":...}`; metadata acknowledgement, not material readiness |
| POST /materials/install | Binding plus `material` | MaterialStatus |
| POST /materials/status | Binding | MaterialStatus for that exact operation/version |
| POST /materials/refresh | Binding plus `material, predecessor` | MaterialStatus; predecessor is exact current receipt ID |
| POST /materials/revoke | Binding | MaterialStatus; use a new operation key and strictly higher fence |

Material is strict base64 for 1–65,536 decoded bytes. Total request body is
bounded at 131,072 bytes before JSON parsing. There is no path, URL, environment
injection or fetch option. File mode is fixed at 0400; mounts are read-only.
Generated directories/slots, never request paths, select private source files.

`MaterialStatus` is `{status, receipt_id, revision, reason}`. No response
contains material, a secret-derived digest, or private source path.

| status | Meaning |
| --- | --- |
| current | Exact receipt finalized, still current, unexpired, current boot, verified private file present at the check |
| not_accepted | `NO_RECEIPT` after successful durable lookup, or `SUPERSEDED` receipt that is no longer current |
| revoked | Durable grant tombstone denies all versions, including pending and delayed requests |
| expired | Exact expiry has passed; resolver access denied regardless of file presence |
| unavailable | Pending installation uncertainty or missing/invalid material; never evidence of nonacceptance |

HTTP 503 `AUTHORITY_UNAVAILABLE` also means unknown acceptance, never
`not_accepted`. Other errors: 404 disabled/no visible preparation, 409 exact
binding/idempotency/version/fence conflict, 413 excessive body, 422 sanitized
invalid schema/material/expiry. Errors never serialize submitted fields.
Identifiers are non-secret controller metadata; do not embed credentials in them.

### Bounded preparation renewal

`PreparationRenewal` is exactly
`{idempotency_key, current, expected_preparation_expires_at, expires_at}`.
`current` is the exact currently installed `Binding`; both top-level timestamps
are timezone-aware. This controller-only operation uses the same expected-identity
headers and body limits as the material API. It neither creates a preparation nor
changes its identity, target, grant, workspace association or execution admission.
Ordinary `/preparations` still rejects a different expiry under the same identity.

Under the existing owner lock, a first renewal requires an unexpired preparation,
an unrevoked, nonsuperseded grant with no pending refresh, the exact current
installed receipt, and matching revision, fence and authorization generation.
Private material presence is checked outside the lock and authority is checked
again under the lock. The preparation cap must equal
`expected_preparation_expires_at`. The new cap must strictly increase and be no more
than one hour ahead of receiver time. The trusted controller must bound that cap
by freshly verified underlying authority, not assume the old short material lease
itself grants an extension. The Core one-hour ceiling is unchanged.

One transaction assigns a fresh preparation JSON value and records a tagged
`prepare_renew` receipt with a SHA-256 of canonical non-secret request metadata.
No new table is required. This receipt is never `Grant.current` or `Grant.pending`,
cannot serve as a material binding or runtime anchor, and retains boot/workspace
metadata for existing cleanup. No bytes, views, grant counters or runtime anchors
are changed. A subsequent ordinary material refresh can extend its own expiry
within the renewed cap, keeping the original workspace preparation/grant anchors.

An exact same-key retry, including after lost response or a later valid material
refresh, returns the recorded acknowledgement without extending again or restoring
old pointers/counters. Current material and owner authority must still be valid;
revoked, expired, superseded or uncertain material cannot be resurrected. Changed
requests and key collisions with material operations conflict in both directions.
Concurrent new keys using the same old preparation cap cannot both commit.

Renewal/refresh response loss never prevents cleanup: `/materials/revoke` still
accepts the same preparation/grant with an old or expired binding expiry and a
fence above current authority, revision at least current, and nondecreasing
authorization generation. Revocation dominates pending or already accepted refresh
and later renewal retries. An absent preparation remains an explicit error, not
evidence that cleanup succeeded. HTTP 503 means unknown acceptance; reconcile or
retry the exact renewal request rather than reassociating the workspace.

## Durability, cutover, expiry and cleanup

PostgreSQL stores only non-secret metadata in independent `hosted_owners`,
`hosted_preparations`, `hosted_grants` and `hosted_receipts` tables.
Legacy metadata-only secret lease reissue cannot clear these tombstones.
Idempotency keys are unique per owner across renewal and material operations and bind the
entire non-secret request plus operation/predecessor. Changed metadata conflicts.
Same-key replay reconciles the original receipt and **never writes bytes again**;
even different valid bytes under that key cannot replace material. Use a new
revision and key to change bytes.

An install reserves intent/high-water authority under a short owner row lock,
releases the database connection, writes a private bounded file, then finalizes
under a fresh identity/authority check. Publication uses an atomic no-replacement
hard link of a private staged file, followed by unlink of the staging name.
It is equivalent to an atomic rename without replacement; no receiver reads
pending versions. A crash leaving two links makes file validation fail closed.
Unresolved intent is never automatically retried or reported as accepted.

Refresh uses a distinct slot and keeps the prior receipt current until successful
cutover. Revision and fence must increase; authorization generation cannot
decrease. Concurrent refresh is rejected while a pending version exists.
Revoke commits a terminal per-grant tombstone before removing owned files.
After cleanup, the revoke receipt records `cleaned` in a separate transaction.
This records that cleanup pass, not a promise about in-flight writes.
Revocation can precede install. A revoked grant is never reusable; a newly
authorized grant requires a new grant ID. Tombstones have no automatic purge.

No row lock or connection spans filesystem work, network calls, or process work.
File operations use descriptor-relative no-follow/exclusive opens and validate
ownership, type, link count, mode and root identity. Cleanup unlinks only generated
slots without following links. Storage is bounded to 1024 preparation directories
and 128 file entries per directory in addition to the tmpfs deployment limit.

Observed expiry is persisted as a terminal receipt state, including if the clock
later moves backward. Status/resolution of expired material attempts cleanup; failure retains denial
and reports `EXPIRY_CLEANUP_PENDING`. Revoke replay retries cleanup without
reissuing authority. Revoked status proves authorization denial, not that an
in-flight unpublished write cannot still finish. Its finalization will fail and
remove its slot. No periodic expiry sweeper or terminal workspace hook is wired
in this foundation: expiry prevents resolution immediately; file reclamation is
on status/resolution, revoke, boot or shutdown.

Startup removes known older-boot slots from its root. Persisted receipts never
prove ephemeral files survived. Shutdown closes admission/resolution, invalidates
current boot authority and attempts owned file cleanup. Crashes do not depend on
shutdown. A superseded process can clean only its own generated slots. Database
or cleanup interruption can leave unavailable material or orphaned private files,
never authorization to resolve old versions.

## Resolver and unimplemented runtime boundary

`ProtectedResolver(database_url, config, identity, bindings)` implements the
existing synchronous `resolve(profile, *, workspace_id)` contract. Its trusted
mapping is declaration name to exact Binding. Only `provider="protected"`,
`kind="mount"`, `mode="ro"`, no `ref`, and an exact target/workspace match are
supported, including for optional declarations. Duplicate targets are denied.

The resolver owns a bounded serial thread/event loop and a loop-local asyncpg
engine. Every resolve queries current durable authority and verifies files.
Call `close()` to release it; asynchronous consumers must use
`asyncio.to_thread`. It returns `LocalSecretLeaseResolution` with private
read-only mounts, empty environment and safe count metadata. It never consults
host home, host environment, node-global auth or local provider fallbacks.
It is tested with actual receiver-installed material, not an uncalled mock.

A mount is point-in-time authorization. **Deleting a file cannot recall a
credential already copied by a workload**, nor revoke a provider credential.
Running-process expiry/revocation, source mount handoff, complete fallback
suppression, checkout/provider-preflight ordering, cancellation and terminal
teardown must be implemented and independently proven after PR1097 settles.
No worker, Compose lifecycle, profile schema, provider semantics, monitor,
scheduler or private Cloud code is changed here.

Migration rollback drops only the four new tables. Rollback is supported only
with protected service stopped and ephemeral storage cleaned; active rollback
destroys denial history and is unsupported. AWF/GitHub own full validation,
coverage, publication and merge gating.
