# Opt-In Node Idle Gate

This is a bounded idle-stop/restart contract, not complete I5 recovery, runtime
activation, a replacement controller, or proof that another VM is fenced.
The stock deployment remains closed until trusted evidence implementations are
supplied. All external effects in the accompanying tests are synthetic.

## Activation And Trust

Apply migration `ca97d3e4f506` to a **dedicated Core node database/schema**. Set
`AWF_HOSTED_NODE` with the existing hosted settings plus:

```json
{
  "owner": "f28674ae-72bc-49df-8a5c-a758e1099f4b/nodea",
  "incarnation": "18446744073709551615",
  "recovery_generation": 1,
  "lifecycle": {"disk_ref": "disk-123", "database_ref": "node-database"}
}
```

The excerpt intentionally omits controller credentials and material-root setup.
Owner, physical ID, generation, retained disk and database association must come
from authenticated server provisioning, never request bodies or caller endpoints.
`incarnation` is a canonical nonzero uint64 decimal string, not a Compute name.
Logical owners are bounded hierarchical strings, not filesystem paths.

Every `Receiver.start`/`boot` advances the persisted boot epoch and closes the
gate, including stop/start on the **same physical VM**. Recovery acceptance is
cleared. Old factories retain the old identity and cannot write. A handler peer
must receive the existing trusted identity, not call `boot` or discover the most
recent identity from the database. `bind_factory(factory, identity)` binds its
transactions to that identity. Worker factories are deliberately not auto-bound.

Existing opted-in ownership/storage cannot be changed or disabled through a
configuration omission. Missing/disabled admission triggers prevent boot. A
migration downgrade refuses to remove an active gate. Offline authority
retirement is a separate, explicit operational procedure.

The database role is trusted application code: do not grant DDL, trigger bypass,
or lifecycle-row administration to tenant callers. This is not a substitute for
database authentication or an external physical fence. Run normal migrations
before starting applications; metadata `create_all` is not production setup.

## Trusted Composition

`Receiver.lifecycle` is a `NodeLifecycle` when configured. Its two asynchronous
callbacks default to **no evidence** and have a five-second await bound:

```python
receiver.lifecycle.verify_recovery = verified_receipt_lookup
receiver.lifecycle.observe_host = trusted_host_observation
```

`verify_recovery(identity, previous_identity, receipt_ref)` returns
`RecoveryEvidence(identity, previous_identity, disk_ref, database_ref,
receipt_ref, expires_at)` only after checking a genuine recovery receipt. It
must establish current authenticated physical identity, actual required prior
incarnation fencing, retained disk/database associations, and reconciliation of
external outcomes. The previous identity includes the previous boot epoch, even
on the same VM or when enabling the gate on an existing hosted database. An
unknown prior VM or ambiguous paid/GitHub outcome must not receive a receipt.
An identity acknowledgement, boot epoch, expired lease, or Google API request
acceptance alone is not this proof.

`observe_host(identity)` returns `HostObservation(identity, expires_at,
quiescent, retained_state_verified)`. It must inspect real node/runtime health,
retained mounts/database identity, processes/containers, and maintenance,
checkpoint, upgrade and recovery activity not represented by the database.
Neither method accepts evidence booleans from an HTTP caller. Receipt references
are opaque lookup keys, not retained claims or authorization in themselves.
Evidence must match exactly and expire within 60 seconds. Exceptions/timeouts
produce no positive acknowledgement and never serialize exception text.

When `AWF_HOSTED_NODE` includes trusted `host` settings, the stock FastAPI
lifespan uses `start_hosted` to install `HostRuntime`'s `observe_host`,
`observe_boot` and `verify_recovery` callbacks. See
[Closed Linux Host Composition](closed-host-composition.md) for the required
provisioning and evidence boundaries. Without `host`, the callbacks still
default to no evidence; there is no automatic activation. A separate worker
process still requires trusted boot identity and a bound factory through its
own composition. Hosted worker execution remains closed. No automatic I3
credential integration or Google verifier is provided here.

## HTTP Contract

All routes are under `/v1/hosted-node/lifecycle`. They require the existing,
separate controller bearer and all four exact expectation headers:
`X-AWF-Expected-Owner`, `X-AWF-Expected-Incarnation`,
`X-AWF-Expected-Recovery-Generation`, `X-AWF-Expected-Boot-Epoch`.
The existing authenticated `/v1/hosted-node/identity` is the discovery route.

| Method And Path | Body | Meaning |
| --- | --- | --- |
| GET `/readiness` | None | Fresh host observation plus serialized DB check |
| POST `/recovery/acknowledge` | `{"idempotency_key":"ack-1","receipt_ref":"receipt-1"}` | Verify exact receipt before opening internal DB admission |
| POST `/drain` | `{"generation":1}` | Close idle admission, then recheck host and DB |
| POST `/drain/abort` | `{"generation":1}` | Persist abort tombstone; never reopen unrecovered boot |

Every success returns `identity`, `state`, `admission_closed`, `runtime_ready`,
`node_ready`, `quiescent`, `database_quiescent`, `idle_since`, `drain_generation`,
and `expires_at`. States are `recovering`, `ready`, `draining`, `drained`.
`runtime_ready` is **always false** in this slice. `node_ready` reports verified
node health with the internal DB gate open; it does not authorize dispatch.
The existing protected runtime mutations still return
`HOSTED_RUNTIME_INTEGRATION_REQUIRED`; material identity acknowledgement still
reports `activation: closed`.

`generation` is a positive signed-64-bit integer, monotonic within the exact
boot identity. Repeating the same recovery key/receipt or drain is safe after
response loss. Conflicting recovery acknowledgements return 409. Abort retains a
durable high-water mark, so a delayed drain cannot re-close an aborted generation
and a late older abort cannot reopen a newer drain. Recovery replay does not
reopen a drain. A lost response is unknown, not acknowledgement.

Missing recovery evidence returns 503. Unknown stored work on recovery returns
409 `RECOVERY_OUTCOME_UNRESOLVED` and remains closed. Stale identities return
403; stale drain generations return 409. Authority/database errors return
sanitized 503. When host evidence is absent after closure, state stays `draining`
and `quiescent` is false; no timeout automatically reopens admission.

## Stop Protocol And Atomicity

Cloud retains authorization, durable demand, owner serialization and the default
**15-minute idle cooldown**. Core does not replace that policy. Every guarded
work mutation resets `idle_since`, so work between observations cannot preserve
an old cooldown. Recovery/reboot also resets the idle interval.

Before stop, Cloud must verify fresh exact-identity readiness and its durable
queue/inflight state, request a drain, and require **all** of: exact current
identity and requested drain generation, state `drained`, `admission_closed`,
`quiescent`, `database_quiescent`, and unexpired evidence. Recheck durable demand
under Cloud's existing stop fence before the provider mutation. New demand must
durably abort the current drain before reopening; do not race a successful abort
against an already-authorized stop. Core cannot make a remote Cloud transaction
atomic with Compute. Do not stop on HTTP 200 alone.

The singleton gate row is the PostgreSQL mutex shared by drain and triggers on
workspace/task/admission, claim, operation, reservation, validation, callback,
merge-candidate, runtime-release-event and secret-lease mutations. Independent
connections and raw SQL updates are covered. If a work transaction wins, drain
waits and sees it before deciding. If drain closes first, the writer fails.
First opt-in activation also waits for existing work-table writers.

Idle requires no nonterminal workspace/attempt, including queued, monitoring,
blocked/human-wait and recovering states; no execution/monitor claim, even if
expired; no unresolved operations/validations, active reservation, pending or
unknown callback/merge/GC/secret state. Terminal Compose runtimes require the
existing effective release event, respecting later revocation. Retained worktree
files alone do not prevent sleep. Unknown states fail closed. Active-work drain
is deliberately unsupported: a busy node remains open and reports not idle.

## Remaining I5/T5/T6 Work

- Real authenticated recovery receipt verification, provider fencing and host
  quiescence implementation, including coordination with untracked host activity.
- Trusted server/API/worker composition and binding of every participating
  process. Database rejection is a fence, not graceful worker pause integration.
- I3 runtime credentials/activation and end-to-end durable REST/MCP wake followed
  by genuine readiness and dispatch. No task or paid execution is demonstrated.
- Reconciliation of pre-existing nonterminal work after a crash. This bounded
  idle-only handshake refuses to open over it; it does not implement a second
  recovery/monitor engine or silently replay an unknown action.
- Real VM stop/start and replacement/failure/old-node-return drills; retained
  filesystem verification, Docker/process tests, T5 full workspace lifecycle,
  T6 failures and D13 timing/observability evidence remain unverified.
