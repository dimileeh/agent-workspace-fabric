# Independent agent and model-provider selection

This release adds T4–T6 catalog and diagnostic workspaces to the merged T1–T3 foundation,
plus live monitor repair/restart identity preservation. Third-party
Codex/Claude admission defaults to disabled for new deployments through
`AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED=false`. This foundation is deployable
before provider compatibility is established. Measured acceptance covers the
exact six tested selections below plus ordinary monitor repair/restart identity,
subject to the proof/opt-in gates documented here; it does not release general
third-party coding compatibility. Provider selection uses REST, CLI or MCP.

## Selection contract

`task.agent` selects the coding CLI. Optional `task.provider` selects the model
service independently; `task.model` selects its exact model. Omitting provider
preserves native Codex/OpenAI and Claude/Anthropic defaults, including `xhigh`.
Cursor, Antigravity, Grok and OpenCode retain their existing selection behavior.
OpenCode continues using its qualified-model provider inference; this release
adds no OpenCode/OpenRouter parity feature.

The console shows the recorded provider beside the agent and model on workspace
cards, in the inspector and task details, and in fullscreen log headers. For
example, `codex · OpenRouter · moonshotai/kimi-k2.7-code` distinguishes the model
service from the model's publisher. Older payloads without a recorded provider
keep their existing labels; the console does not guess from the model name.

Codex and Claude can describe `ollama` or `openrouter` selections. Such requests
require an explicit model and `preflight.allow_unverified_model=true` for models
without coding-compatibility evidence. That flag defaults to false. Known
embedding/reranking models fail even with experimental opt-in. Third-party
effort remains absent; explicit effort is rejected until supported evidence is
available. Native defaults cannot be reinstalled by the adapter registry.
Existing fallback `target.provider` is authoritative and each declared target
is frozen as a complete selection at creation.

The creation options are REST `task.provider` and
`preflight.allow_unverified_model`, CLI `workspace create --provider` and
`--allow-unverified-model`, and MCP `awf_create_workspace` parameters `provider`
and `allow_unverified_model`. Old requests remain valid. Adoption does not
accept new independent-provider fields.

`task_policy.agent_execution` freezes contract version **1**, agent, provider,
model, effort, explicitness, endpoint, credential reference, sidecar service
identity and compatibility status. New native selections are frozen too.
Rows without this field retain deliberate legacy inference. A present corrupt
or unknown snapshot fails with `EXECUTION_CONTRACT_UNSUPPORTED`; it never
falls through to native execution. The snapshot follows planning, execution,
repair, monitor reconstruction, restart/resume and recovery through existing
adapter defaults. Workspace, task, attempt and recovery projections expose it.
Unknown third-party pricing remains unknown; native profile prices are not
applied to another selection. With managed OpenRouter keys, OpenRouter runs
report exact provider spend instead (see
[Managed OpenRouter keys and budgets](#managed-openrouter-keys-and-budgets)). Native usage collectors are not used to invent
third-party token/cost data.

```mermaid
flowchart LR
  S[Frozen selection v1] --> C[Worker and runtime capability admission]
  C --> I[Workspace config and selected credential references]
  I --> N[Actual agent-network readiness]
  N --> E[Chosen endpoint and coding CLI]
  E --> R[Same selection retry or complete declared fallback]
  R --> S
```

## Connections and isolation

Both local Ollama models and `:cloud` models use the configured Ollama daemon.
AWF does not call `ollama.com` directly or copy the daemon's credential store
into an agent. URL precedence is profile before service environment; within
each source it is `AWF_OLLAMA_BASE_URL`, legacy
`AWF_OPENCODE_OLLAMA_BASE_URL`, then `OLLAMA_HOST`. Port defaults and Docker host
gateway normalization remain consistent with the legacy Ollama path. A single
normalized daemon origin preserves path prefixes and removes a trailing `/v1`.
Explicit malformed URLs, URL userinfo, query parameters and fragments are
rejected without echoing their contents.

OpenRouter has the fixed origin/prefix `https://openrouter.ai/api`: Codex uses
`/v1/responses`, Claude `/v1/messages`. Credentials use an
`env/OPENROUTER_API_KEY` reference, a profile environment alias, or an existing
declared env secret lease. Inline model-service credential values in expanded
runtime configuration are rejected. Snapshots, argv and hosted payloads contain
references, never credential values. Secret rotation behind the same reference
does not alter connection identity.

Auto-profile requests defer endpoint, credential-reference and network-service
snapshots until provisioning resolves the repository profile. The primary and
all declared fallbacks bind together and commit atomically with the resolved
profile before stack launch, under the existing port-admission and claim locks.
Worker loss before that commit leaves both unbound. Afterward, credential
rehydration rejects changed repository profiles or credential aliases with
`EXECUTION_PROFILE_CHANGED`; unchanged profiles retain the frozen connection even
if worker defaults change. A competing resolver stops without replacing or
failing the winning binding.
Native selections carry a pending-default marker until the executor freezes its
configured model/effort defaults; explicit request fields win, and restarts keep
the authoritative values. Initial feature/release sync monitors freeze before
their first adapter, preserving that selection through restart. OpenCode derives
its provider from the effective model and rejects a configured model that
conflicts with an explicit provider constraint. Host-local endpoint normalization
reuses the existing IPv4 loopback, expanded IPv6, mapped IPv4 and
unspecified-address policy.

Third-party stacks suppress native auth mounts and competing model-service env,
including Claude backend toggles and role-model variables. Native selections
retain their own backend configuration and auth while omitting fallback-provider
material. Ollama stacks omit
OpenRouter credentials from ambient auth, profile env and declared leases before
rendering the container environment. OpenRouter materialization and execution
passthrough use the frozen credential reference; separate tool auth is retained.
The declared auth resolver copies only allowed instruction/tool configuration
into a workspace owned, read-only source file under the existing `auth/<workspace>` cleanup
root. Source auth/config is unchanged. A standalone stdlib runner in the pinned
agent image creates a distinct config for each invocation under the invoking
user's home at `~/.awf/provider-config` (`/home/agent/.awf/provider-config` in the
standard agent container), outside OS temporary storage. It injects only the
selected model credential and removes the config after completion, failure or
TERM cancellation. Codex uses a custom Responses provider and an
untrusted project-config layer. Claude loads only the `project` setting source
(project `CLAUDE.md`, rules, skills, agents and settings) together with
`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`, so settings-file endpoint, auth, model
and role-model env are ignored (the CLI logs a warning); `user`/`local` sources
are never loaded. See [Runner parity](#runner-parity-role-models-retries-project-instructions).
Safe instructions and allowed tool/permission configuration remain available.
The runner scrubs the selected credential before forwarding stdout/stderr.
Hard-killed containers remain subject to AWF's ordinary container cleanup.

Monitor resume resolves the promoted selection's actual auth mounts and provider
environment before recreating the agent. This covers native/third-party changes
and Ollama/OpenRouter changes across both supported agents. Fallback credentials
are not preloaded. Companion services, database values and Compose interpolation
remain intact; materialization failures stop the resume with a durable reason.

The runtime probes the selected endpoint/model from its own network before each
invocation. Readiness is separate from coding compatibility. Creation records
`EXECUTION_NETWORK_PROBE_PENDING` and defers that network observation; a host
request never certifies a future sidecar. Enabled ordinary admission requires
exact successful diagnostic evidence or `preflight.allow_unverified_model=true`.
Auto-profile evidence lookup waits for atomic connection binding. The exact
image ID, CLI version, model revision when available, selection, connection scope
and fixture version are checked again before each proof-authorized invocation,
including monitor repairs. Explicit experimental opt-in still requires runtime
isolation and endpoint readiness. Admission remains disabled by default.

Unknown runtime images fail with `EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED` before
a coding CLI launches. Managed provider configuration
(`/etc/claude-code/managed-settings.json`, `/etc/codex/config.toml`) outranks
every isolation layer and remains explicitly unsupported
(`EXECUTION_PROJECT_CONFIGURATION_UNSUPPORTED`). Project Claude agent definitions
(`.claude/agents`) are supported: subagent and role models are forced to the
frozen selection and their endpoint/auth stays host-managed. Custom auth
resolvers lacking safe-config preparation fail explicitly. Hosted request
serialization carries the complete selection; injected executors must advertise
`execution_contract_version=1` and enforce equivalent isolation. The built-in
hosted delegate does not advertise that guarantee and rejects expanded work;
native hosted behavior remains available.

## Runner parity (role models, retries, project instructions)

Third-party runs keep the native CLI behavior that matters for long coding
tasks, without giving up the selected connection. Native selections never use
the runner and are unchanged.

**Claude role and subagent models.** Claude Code sends role aliases (background
work, built-in subagents such as Explore) and subagents to their own model ids.
Against a third-party endpoint those native ids fail on Ollama, and fail or bill
a different model on OpenRouter. The runner therefore sets, for the frozen
selected model:

| Variable | Value |
| --- | --- |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL` | selected model |
| `CLAUDE_CODE_SUBAGENT_MODEL` | selected model |
| `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | `1` (agent frontmatter and Agent-tool `model` are ignored) |
| `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` | `1` |
| `CLAUDE_CODE_EFFORT_LEVEL` | `auto` (no effort on the wire, even if project settings declare `effortLevel`) |

These are added to `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_API_KEY=""` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. Every
inherited `ANTHROPIC_*`/`CLAUDE_*` value is removed first, so only runner-chosen
values reach the CLI.

**Project instructions.** Claude runs with `--setting-sources project` and the
generated `--settings` file (selected model plus safe permission/language keys),
which outranks project settings. Host-managed provider mode makes the CLI drop
settings-sourced endpoint, auth, model, role-model and subagent-model env, so a
project `.claude/settings.json` cannot redirect the run. The generated file's
`env` also repeats the non-secret forced values (host-managed mode, role and
subagent models, subagent forcing, `CLAUDE_CODE_EFFORT_LEVEL=auto`), so project
settings env cannot disable forcing or re-add effort either. The selected auth
also wins over a project `apiKeyHelper`.

**Codex retries.** The generated `config.toml` sets `request_max_retries = 4`
and `stream_max_retries = 5`, which are Codex's own defaults. With zero retries,
one transient 5xx or stream drop ended a multi-hour run. Each retry/reconnect is
a CLI notice in the redacted AWF log stream; exhausted retries still exit
non-zero with the existing reason-code mapping. These transport retries happen
inside one invocation and are not AWF invocation replays, so AWF still never
retries diagnostic invocations.

**Credential withheld from tool subprocesses.** Host-managed mode makes Claude
remove `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` from Bash, hook and tool child
environments; declared tool env (for example `GH_TOKEN`) is kept. Codex's
generated config adds `[shell_environment_policy] exclude =
["AWF_SELECTED_PROVIDER_KEY"]`, independent of upstream default excludes.
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is deliberately **not** set: in Claude Code
2.1.295 it aborts startup unless bubblewrap is installed (the agent image has
none, and Docker's default seccomp blocks the user namespaces it needs), forces
the default permission mode even with `--dangerously-skip-permissions`, and
writes stub files into the worktree. Project MCP server children are assumed to
use the same child-env builder; that is not separately proven.

**Proof.** `tests/integration/runtime/test_claude_provider_runner_contract.py`
runs the real standalone runner and the pinned Claude CLI against a localhost
fake Anthropic endpoint, with a hostile project `.claude/settings.json`
(decoy endpoint, project token, `apiKeyHelper`, model/role/subagent overrides,
`effortLevel`) and a project agent pinned to a native model. It asserts that the
project `CLAUDE.md` and agent prompt reach the endpoint, every request uses the
selected endpoint, credential and model, the decoy receives nothing, no effort is
sent, and the Bash tool's environment lacks the credential. It skips unless
`claude --version` is 2.1.295. Unit tests pin the exact environment, argv and
Codex config.

**Evidence validity.** The four third-party deployed passes below were measured
with the previous runner (the two native passes do not use it), all on image
`sha256:8e0f082a466be412010ec9f17f7ee5622903b229760b68507076aef844d4b04d`.
They remain accurate historical evidence for that image. Admission matches
evidence on the exact `runtime_image`, so a rebuilt image carrying this runner
does not inherit them: after upgrading, re-run `awf provider test` for each
selection or use `preflight.allow_unverified_model`. The Claude wire request
changes (role models, no effort field), which is why fresh evidence is needed.
The proof contract (fixture, verifier, ordered witness) is unchanged, and the
diagnostic fixture worktree contains no `CLAUDE.md` or `.claude/`, so the
fixture stays `coding-cycle-v1`.

## Recovery and cooldown scope

Migration **85ce3f9b2a61** follows **c6d7e8f9a0b1**. It preserves existing
cooldown history in the fixed `legacy` scope and replaces pair uniqueness with
`(provider, model, connection_scope)`. The scope hashes normalized endpoint and
credential-reference identity. Independent sidecars additionally use service
and workspace namespace; a new attempt binds to its own namespace. Shared
remote servers retain shared scope. No secret values or token hashes enter the
key. Scheduler, capacity and monitor suppression share one resolver and retain
batched candidate queries. Concurrent failure increments serialize per scope.

Same-choice retry retains the entire selected snapshot. Explicit fallback
promotes the target's entire snapshot for both a new attempt and an in-place
monitor repair, clearing incompatible effort and Cursor Auto selection. An
approved planning-scope model fallback also updates the frozen selection used by
executor helpers and adapter reconstruction, preserving provider, endpoint,
credential reference and native effort. OpenCode model promotion recomputes its
provider and honors an explicit provider constraint. New third-party primaries and declared fallbacks remain blocked while admission
is disabled. Enabled selections reach the actual runtime-network probe. An uncontracted third-party fallback is
rejected. Cross-choice native fallback is never inferred for a third-party
selection. Disabling admission blocks new
third-party fallback promotions as well as creation; already selected work
retains its identity while draining or parked.

Schema/execution-v1 rollback floor: **`8322f8c33eec252d86a1ee5e95f5f313111abd9f`**
(merged repaired foundation, PR #1199). Once diagnostic rows exist, the operational
rollback floor is **`99bf2d1f049ef87f78271568ed65c675d523b5ee`** (fully repaired
merged feature, T4–T6, worker capability 2, fixture v1). The foundation alone
cannot resume live diagnostics.

Workers in this release advertise capability **2** (execution selection remains v1). Expanded admissions require every live queue consumer at capability 2; legacy rows backfill to
**0**, meaning unsupported. Admission requires every live queue consumer to be
compatible, and dispatch rechecks capability. This metadata cannot make an
already-running old binary safe. Follow the mandatory [upgrade and rollback
sequence](UPGRADE.md#provider-foundation-compatibility-floor).

## Managed OpenRouter keys and budgets

Third-party OpenRouter runs normally share one static `OPENROUTER_API_KEY`, so
their spend is unattributed and unbounded. Configuring an OpenRouter
**management** key turns on exact per-workspace cost tracking and budgets through
OpenRouter's key-management API (`/api/v1/keys`).

**Opt-in.** Set `OPENROUTER_MANAGEMENT_KEY` in the service environment. Compose
forwards it (with `AWF_OPENROUTER_WORKSPACE_LIMIT_USD`) to the `api` and `worker`
services only; never to `migrate`, the console or any workspace stack. Both
processes remove it from their own process environment once settings resolve,
so Docker Compose, git and agent subprocesses cannot inherit or interpolate it.
Profile environment values, service/companion environment and env-secret leases
that reference it are rejected with `PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN`.
It never enters hosted payloads, logs, argv or the database, and service config
payloads and support bundles redact it. Without a management key nothing
changes: the static key, unknown pricing and today's stacks stay as they are.

**Selection identity.** With a management key, every OpenRouter selection
frozen at creation uses the fixed credential reference `managed/openrouter`.
This covers primaries, declared fallbacks, auto-profile bindings, monitor
repairs and diagnostics. The connection and cooldown scope is the same for every
managed selection and never fragments per workspace or per key. Toggling the
management key later does not change existing rows: a managed row on an API or
worker without the key parks with `PROVIDER_MANAGEMENT_KEY_MISSING` and never
falls back to the static key. Managed admission requires every live queue
consumer at worker capability **3**. Ollama, native and OpenCode selections are
unchanged. Hosted runtimes reject managed selections
(`PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED`).

**Budget.** The budget is frozen beside the selection in
`task_policy.provider_budget`:

```json
{"version": 1, "provider": "openrouter", "managed": true,
 "limit_usd": 10.0, "requested_usd": null, "source": "default"}
```

- The default comes from `AWF_OPENROUTER_WORKSPACE_LIMIT_USD` (10 USD).
- Override it per task with REST `task.provider_budget_usd`, CLI
  `workspace create --provider-budget-usd` or MCP `awf_create_workspace`
  `provider_budget_usd`.
- A budget without a managed selection is rejected with
  `PROVIDER_BUDGET_UNSUPPORTED`.
- Connection tests run under a fixed 1 USD budget.
- Idempotent replays compare the requested budget.

**Key lifecycle.**

- **Mint.** Just before the selected credential is materialized for an agent
  invocation, AWF mints `awf-<workspace>-<seq>` with `include_byok_in_limit=true`
  and a limit equal to the remaining budget: the budget minus the spend of
  finalized keys, minus the full limit of keys that might still be live.
- **Inject.** The value is passed by name only (`docker compose exec -e
  OPENROUTER_API_KEY`) to the runtime probe and the isolated runner as the
  selected credential. It never enters `compose.yml`, the container config or
  argv. Managed stacks render no OpenRouter credential at all.
- **Heartbeat.** During the invocation AWF reads the key's usage on a bounded
  cadence (`openrouter_usage_poll_interval_seconds`, default 120 s).
- **Release.** Afterwards AWF reads the final usage, persists it, and then deletes
  the key. A cancelled caller cannot strand a key.
- **Replacement.** Container recreation, monitor resume and worker restart always
  lead to a new key. Under the workspace claim, a key (or hashless minting intent)
  the worker does not hold is dead: it is finalized (or released) before the next
  mint, even while its lease is unexpired.
- **What is stored.** AWF persists only the non-secret identity (name, hash, seq,
  limit, usage, BYOK usage), never a key value. Spend is `usage + byok_usage`,
  summed across all of a workspace's keys.

**Exact spend.** Workspace show/list, overview, the task list, the websocket
snapshot and MCP report `provider_spend`
(`kind: exact_provider_spend`, spend, budget, remaining, key counts, last read).
For managed runs `llm_usage` reports that exact spend with source
`openrouter_key_usage`, labelled **Exact provider spend** in the CLI and console,
instead of `unknown`.

**Budget exhaustion.** A failed run whose final key read shows no remaining limit
becomes `PROVIDER_BUDGET_EXHAUSTED`. Otherwise only the CLI's own error counts: an
HTTP 402 / limit-reached message in the tail of stderr. If the key read still shows
headroom, that 402 is the OpenRouter account balance, not the workspace budget,
and becomes `PROVIDER_ACCOUNT_CREDITS_EXHAUSTED` (top up the account, then re-set
the budget to resume). If the key read failed, it stays `PROVIDER_BUDGET_EXHAUSTED`.
Payment text in tool output or the transcript never counts. The workspace is
parked (`blocked`, block type `provider_budget`) at both failure sinks (executor
and PR monitor):

- It is never failed.
- It is never retried blindly.
- It never falls back to another provider.
- It is never counted against the shared circuit breaker.

A mint failure (`PROVIDER_MANAGED_KEY_UNAVAILABLE`) parks only that selection.
So does a mint that only headroom still held by keys AWF could not finalize yet
blocks (spend alone leaves budget); its park event reports `reserved_usd`.
Raise the budget to resume:

| Intent | REST | CLI | MCP |
| --- | --- | --- | --- |
| Set budget and resume | `POST /v1/workspaces/{id}/provider-budget` `{"usd": N}` | `awf workspace provider-budget <ws> --usd N` | `awf_set_workspace_provider_budget` |

The control requires an `Idempotency-Key` (`If-Match` is optional). It works as
follows:

1. It updates the live key's limit before any state change. A management-API
   failure returns 502 with its reason code and leaves nothing changed.
2. It freezes the new budget (`source: operator`) and records
   `provider.budget.raised` plus a control audit event.
3. It resumes the park: a pre-PR park re-invokes the agent with a "provider budget
   raised" directive, and a monitor-origin park returns to `monitoring_pr`.

The new budget must exceed the recorded spend. Diagnostics never park: exhaustion
is their terminal outcome.

**Cleanup and reconciliation.** When a workspace's runtime is released at a
terminal state, AWF final-reads and deletes its keys. A bounded periodic
reconciler (`openrouter_key_reconcile_interval_seconds`, default 900 s) does the
following:

- It lists `awf-*` keys page by page.
- After recording their usage, it deletes every key with no live binding:
  orphans from crashes, terminal workspaces, failed deletes and unbound mints
  older than a 10-minute grace window. An active key stays live while its lease
  is unexpired or its workspace holds a live execution or monitor claim. Each
  deletion emits `provider.managed_key.orphan_deleted`
  (`PROVIDER_MANAGED_KEY_ORPHAN_DELETED`); a database error on one key is counted
  as failed without aborting the sweep.
- It skips and counts keys of workspaces unknown to this control plane; it never
  deletes them.

Management-API failures are reason-coded (`PROVIDER_MANAGEMENT_API_*`) and never
block unrelated work.

## Discovery and connection tests

| Intent | REST | CLI | MCP |
| --- | --- | --- | --- |
| Catalog claims | `POST /v1/providers/models/query` | `awf provider models` | `awf_query_provider_models` |
| Coding diagnostic | `POST /v1/providers/connection-tests` | `awf provider test` | `awf_test_provider_connection` |

Both accept `agent`, `provider`, optional `profile_ref` or inline `profile`.
Diagnostics additionally require `model`; `effort` follows normal compatibility
rules. Native catalog discovery is explicitly unsupported. Ollama uses only the
configured daemon's `/api/tags`; OpenRouter uses its official `/api/v1/models`.
Discovery never pulls models or invokes an agent. HTTP is bounded to 10 seconds,
2 MB and 5,000 items, with redirects refused. A bounded 128-entry, five-minute
cache uses connection/credential-reference and agent/profile context. No token
values or token hashes identify cache entries. Model capabilities/pricing are
provider claims, nullable when absent. Discovery failure retains manual entry;
it supplies no coding-compatibility proof. Sidecar catalog discovery explicitly
returns `PROVIDER_DISCOVERY_NETWORK_UNSUPPORTED` because no disposable discovery
network transport is available.

Connection tests require `Idempotency-Key` (MCP/CLI `idempotency_key`). Identical
requests return the same workspace/operation; changed diagnostic identity,
profile, deadline or configured runtime returns `IDEMPOTENCY_CONFLICT`. Deadlines
default to 180 seconds, maximum 900. A test request authorizes its candidate
model while ordinary admission is disabled; no second experimental flag is needed.
Known incompatible agent/model/effort combinations still fail.

Diagnostics use normal queue capacity, claims, operations, logs and cleanup.
They branch before project checkout/hooks, forge access, planning, phases,
validation commands and PR lifecycle. The repository identity
`awf-internal://provider-diagnostic/v1` is solely an internal managed linked
fixture worktree. Selected model sidecars may be reproduced only without builds,
project binds, volumes, fixed ports, privilege, dependencies or secret env.
Unsupported profiles fail explicitly; no replacement host endpoint is chosen.
The internal mirror is mounted read-only; no diagnostic commits are needed.
Before creating a new fixture worktree, bootstrap repairs an empty internal
mirror or a missing fixture ref under the mirror lock. Existing refs and
workspaces are preserved; this repair never replays a diagnostic invocation.
Forge/SSH/git auth and unrelated model settings are omitted. Native diagnostics
receive only selected native credential references and isolated credential-only
writable homes for CLI state/refresh, without inherited model/tool config; third-party
invocation homes remain outside OS temporary directories.

The `coding-cycle-v1` fixture asks for one pure function using a fresh challenge
nonce. The immutable challenge/verifier live under `/opt/awf`, outside writable
files. Runtime admission requires a nonroot CLI user and root-owned, unwritable
oracle/interpreter paths. Python wrappers use the pinned absolute interpreter
with isolated startup, so user-site code cannot replace the verifier. The verifier permits literal constants, a pure `answer(value)` function,
concatenation/f-strings and reverse slicing; imports, calls and executable module
statements fail. This keeps candidate code from replacing the oracle or forging
its output. A pass requires ordered, completed top-level typed CLI events with
distinct call IDs: challenge result first, then a tool input containing its exact
nonce. Nested JSON in tool output, prose, exit zero, a combined challenge/edit
command, missing/truncated output, or edited tests cannot establish a pass.
AWF independently verifies the fixture after CLI termination.

Read the workspace's `task_policy.provider_diagnostic`, operation result, events
and existing log surfaces. Results distinguish endpoint health, protocol witness,
fixture verification, a bounded four-event ordered witness with call IDs and nonce-dependent input excerpt, exact selection/model revision, CLI version and actual
runtime image ID. Unknown upstream revisions remain null. Results survive
fixture disposal. Cancel/stop/destroy terminate the runtime before disposal,
including cancel with `stop_stack=false`; teardown failure retains the fixture.
Ordinary retry/refresh/validate/rebase/remonitor/guide controls are rejected.
The durable invocation-start marker prevents paid replay after worker loss:
recovery fences the old owner, stops its runtime and records inconclusive.
AWF never retries diagnostic invocations, falls back, pushes or opens PRs.

```mermaid
flowchart LR
  Q[Selected connection query] --> C[Bounded catalog claims]
  T[Idempotent connection test] --> W[Isolated diagnostic workspace]
  W --> N[Fresh challenge tool result]
  N --> D[Later tool input depends on nonce]
  D --> V[Trusted fixture verification after CLI exit]
  V --> E[Exact runtime and connection evidence]
  E --> A[Ordinary admission remains gated]
  W --> L[Worker loss: stop and mark inconclusive]
  V --> X[Retain result and dispose owned resources]
```

## Remaining release gates and preliminary evidence

Deployed-AWF coding compatibility is now measured for exactly the six tested
selections below plus monitor repair/restart identity: all six deployed diagnostics
in the pinned runtime
(`sha256:8e0f082a466be412010ec9f17f7ee5622903b229760b68507076aef844d4b04d`)
passed with reason `PROVIDER_DIAGNOSTIC_PASSED`, verified fixture
`coding-cycle-v1` and passed protocol evidence (supervisor evidence captured
2026-11-10T02:57:05Z). Live monitor repair/restart identity dogfood was then
performed on this PR: the worker was restarted while the monitor was idle, the
workspace identity and connection scope were preserved, and the subsequent
repair invocation re-selected the same Codex/OpenRouter/`moonshotai/kimi-k2.7-code`
contract (supervisor evidence captured 2026-11-10T03:07:44Z). Local Ollama is implemented and covered deterministically,
**not live-verified**, and excluded from the live release gate by explicit
operator instruction. Do not invoke local inference or change the unrelated
host llama-server.

These measured passes cover only the exact agent/provider/model selections
listed; they do not establish that all provider-catalog models are
coding-compatible. Selection stays explicit through REST `task.provider`/
`task.model`, CLI `workspace create --provider`/`--model`, and MCP
`awf_create_workspace` parameters. Omitting provider preserves the native
Codex/OpenAI and Claude/Anthropic defaults. Any other model still requires
exact successful diagnostic evidence or `preflight.allow_unverified_model=true`;
that experimental opt-in does not imply general catalog support.

| Path | Preliminary CLI-only observation | Deployed AWF acceptance |
| --- | --- | --- |
| Codex / Ollama cloud daemon / `kimi-k2.7-code:cloud` | Dependent tools and fixture observed; return-code metadata missing | **Passed** — codex-cli 0.162.0, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, model revision `eda07a6592375dcbde7cf167b6d6b368cdd28e244f9d71559fb59919aca882fa`, evidence workspace `ws_3c4b15e47f034356973a527f`, connection scope `1098d60c…362f` |
| Claude / Ollama cloud daemon / `kimi-k2.7-code:cloud` | Dependent tools and fixture observed | **Passed** — Claude Code 2.1.295, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, model revision `eda07a6592375dcbde7cf167b6d6b368cdd28e244f9d71559fb59919aca882fa`, evidence workspace `ws_286649acb2d04a29a3301ed8`, connection scope `1098d60c…362f` |
| Codex / OpenRouter / `moonshotai/kimi-k2.7-code` | Dependent tools and fixture observed | **Passed** — codex-cli 0.162.0, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, no upstream revision pinned, evidence workspace `ws_e13cf39dd6f8484ba824e423`, connection scope `c24e8d23…7fa3` |
| Claude / OpenRouter / `minimax/minimax-m2.7` | Dependent tools and fixture observed | **Passed** — Claude Code 2.1.295, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, no upstream revision pinned, evidence workspace `ws_d3b54742506044039b37b9ca`, connection scope `c24e8d23…7fa3` |
| Codex native | CLI 0.162.0 observed | **Passed** — codex-cli 0.162.0 / `gpt-6.1-sol`, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, no upstream revision pinned, evidence workspace `ws_ef1c0b2d51894d8bb2c81af9`, legacy scope |
| Claude native | CLI 2.1.295 observed | **Passed** — Claude Code 2.1.295 / `claude-opus-5-5`, `PROVIDER_DIAGNOSTIC_PASSED`, fixture `coding-cycle-v1`, no upstream revision pinned, evidence workspace `ws_c45f6f1c738e4cb59fb1349c`, legacy scope |
| Monitor repair/restart identity | Live supervisor dogfood | **Passed** — worker restarted while monitor idled on `WaitForCI` (iter 0, head `1922c8634dcbf2fb05ac9761dd25a725e60f91dc`), no active CLI confirmed (`True`); workspace `ws_b8779b060a6d4e2da857074b` identity unchanged (`True`) and connection scope `c24e8d23…7fa3` preserved. Repair invocation at `2026-11-10T03:07:44.986923+00:00` re-selected Codex/OpenRouter/`moonshotai/kimi-k2.7-code` with `effort=null`, endpoint `https://openrouter.ai/api`, credential ref `env/OPENROUTER_API_KEY`, runtime `sha256:8e0f082a466be412010ec9f17f7ee5622903b229760b68507076aef844d4b04d` |

### Re-verification after the PR #1204 runner change (2026-11-10)

- **Why the old evidence no longer counts.** PR #1204 changed the third-party runner
  (Claude role/subagent model forcing, project instructions loaded under host-managed
  provider mode, Codex transport retries, credential withheld from tool subprocesses).
  As documented, evidence from the earlier runtime image therefore does not carry
  forward.

- **What was re-run.** After the runtime image was rebuilt with #1204, six deployed
  diagnostics were run on a local AWF Core control plane (development `a0d7a2f9`). All
  of them passed with `PROVIDER_DIAGNOSTIC_PASSED` on fixture `coding-cycle-v1`:

| Agent | Provider | Model | Evidence workspace | Result | CLI |
| --- | --- | --- | --- | --- | --- |
| claude_code | openrouter | `moonshotai/kimi-k2.7-code` | `ws_39dd1f76affd4c4e8c2df35e` | PROVIDER_DIAGNOSTIC_PASSED | 2.1.295 (Claude Code) |
| claude_code | openrouter | `z-ai/glm-5.3` | `ws_81a1142a3b9541018808803d` | PROVIDER_DIAGNOSTIC_PASSED | 2.1.295 (Claude Code) |
| claude_code | ollama | `kimi-k2.7-code:cloud` | `ws_aaf37c484eeb4a70ac511831` | PROVIDER_DIAGNOSTIC_PASSED | 2.1.295 (Claude Code) |
| claude_code | ollama | `glm-5.3:cloud` | `ws_168cc073fa33410cb08169c3` | PROVIDER_DIAGNOSTIC_PASSED | 2.1.295 (Claude Code) |
| codex | openrouter | `moonshotai/kimi-k2.7-code` | `ws_b4b7a623fdbd42928939e197` | PROVIDER_DIAGNOSTIC_PASSED | codex-cli 0.162.0 |
| codex | ollama | `kimi-k2.7-code:cloud` | `ws_c2d409984d68421d9f6d1f45` | PROVIDER_DIAGNOSTIC_PASSED | codex-cli 0.162.0 |

- **What this does and does not establish.** These passes cover only the exact
  selections listed. `z-ai/glm-5.3` and `glm-5.3:cloud` are newly verified selections.
  Local Ollama models (non-`:cloud`) remain not live-verified.

- **Admission.** Ordinary third-party admission was then enabled on that control plane
  for a controlled canary workspace (this change was itself produced by the canary).

The Codex/OpenRouter Minimax preliminary attempt regenerated its nonce and failed
the fixture. Its transcript remains a negative regression; like every
preliminary observation above, it is a non-deployed CLI-only record, distinct
from the deployed AWF passes and not evidence of deployed acceptance. Source runtime pins
and locally observed versions are Codex **0.162.0**, Claude **2.1.295**; these do
not identify a deployed image. Sanitized preliminary envelopes are deterministic
parser fixtures, never live AWF passes. The supervisor-provided preliminary image
was `sha256:c7d73ed4150e2e1c274e9b90b3b128e6f35a90537ada7033d62a3f07cbc5f39e`,
with wrapper commit `526823f0842009f1abadbc8965ddb0dfcf1939a8`; this is not a
deployed diagnostic-runtime identity. OpenRouter credentials are configured
and `/api/v1/key` was reported authenticated (HTTP 200); this is auth health only.
Cloud descriptor/catalog IDs cannot establish immutable upstream model weights.

AWF/GitHub own broad validation and the authoritative 99% coverage gate. Focused
checks and unmet release evidence are recorded in ignored local validation plans.

Provider configuration references: [Codex advanced configuration](https://developers.openai.com/codex/config-advanced/),
[Ollama Claude integration](https://docs.ollama.com/integrations/claude-code),
[OpenRouter Codex](https://openrouter.ai/docs/cookbook/coding-agents/codex-cli),
[OpenRouter Claude](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration),
and [Claude settings](https://code.claude.com/docs/en/settings).


## PR 1199 regression repairs

Local service Compose forwards `OPENROUTER_API_KEY` and `AWF_OLLAMA_BASE_URL`
to both API and worker, using empty defaults. Declared AWS/Google tool settings
(such as regions, profiles and application credentials) survive provider
isolation; model-backend overrides and role settings are still removed.

The runtime bounds pipe draining after CLI exit and terminates the CLI process
group on completion or cancellation, including inherited writers that ignore
TERM. Temporary invocation configuration is removed after shutdown.

A corrupt or unknown execution snapshot fails only its workspace during
scheduling, with a version fence protecting a concurrently repaired row.
Capacity excludes that row; list/show/overview expose
`agent_execution.reason_code=EXECUTION_CONTRACT_UNSUPPORTED` with unavailable
identity and pricing, without inferring a native selection.

Native hosted monitor resume works without Compose. Expanded hosted selection
without materialization remains rejected. Existing Compose refresh replaces the
selected environment and mounts before recreation. OpenCode configured defaults
and recovery models keep their provider/model pair coherent.

Profiles with host ports defer binding/publication until preflight and companion
checks finish, then freeze the profile and execution snapshots in the existing
locked admission transaction. Port-free profiles retain early atomic binding
for worker-replacement safety.

Focused regressions cover these repairs. Full validation, coverage, review
replies, push and merge remain owned by AWF/GitHub. The six deployed compatibility passes and the live monitor repair/restart identity proof are recorded above.
