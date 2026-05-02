# AWF Pre-GKE Industrial Readiness Checklist

Last updated: 2026-05-02

This checklist is the standing plan for moving AWF from a strong local
agent-workspace fabric into a robust, open-source-ready local Core that is
worth using on an engineer's computer before GKE deployment design begins. It
is based on the current codebase compared against `docs/awf_prd_v2.2.md`.

## Current PRD Alignment

- Full PRD end-state, including GKE, multi-node scheduling, and production
  security: about 70% implemented.
- Non-GKE local industrial target: about 78-82% implemented.
- Phase 1 / Phase 1.5 foundation: about 85% implemented.

Interpretation: AWF is now a real local control-plane and workspace substrate,
not a prototype script. The remaining work is mostly reliability, merge safety,
validation policy, security, and production operations.

## How To Use This File

- Treat checked items as implemented enough to build on.
- Treat unchecked P0/P1 items as blockers before GKE discussion. The bar is:
  AWF Core should be superbly reliable as a local open-source developer tool
  before we shift attention to Kubernetes/GKE design.
- Treat the Active / Completed Slices ledger as the durable anti-duplication
  record for AWF dogfood tasks. Do not launch a new workspace for a slice that
  is already `running`, `monitoring_pr`, or `merged`.
- Update this file whenever a PR lands that materially completes an item.
- Prefer AWF dogfood delivery with `auto_merge=true` for non-`main` targets.
- Keep TDD mandatory and keep AWF self-development coverage at 99%+.

Priority key:

- P0: urgent Core reliability blockers; finish first.
- P1: local open-source Core readiness blockers; finish before GKE discussion.
- P2: GKE design and deployment work; do not start until P0 and P1 are complete.

## DX Review Notes

Plan DevEx review on 2026-05-02 classified AWF Core as a developer-facing
platform with CLI, REST API, MCP, console, docs, and future SDK/client surfaces.
Primary persona for the local open-source Core release: an experienced
platform/product engineer evaluating whether AWF is trustworthy enough to run
multiple coding agents on their own repository. Their tolerance is roughly five
minutes to first proof and one terminal session before they decide whether AWF
is real.

Competitive benchmark: current parallel-agent tools such as Coder Mux,
Stoneforge, webmux/workmux, and SwarmClaw emphasize fast workspace creation,
visible agent activity, and copy-paste onboarding. AWF's differentiator is not
"launch many agents"; it is integration trust: isolated workspace lifecycle,
profile-driven validation, PR monitor discipline, reason codes, retry lineage,
cleanup, and release-readiness evidence. The backlog below adds P1 DX gates so
that value is visible immediately instead of buried in deep docs or operator
history.

## Active / Completed AWF Slices

Status values:

- `requested` / `provisioning`: workspace has been accepted and is starting.
- `running`: workspace is actively implementing the slice.
- `validating`: workspace is in validation or validation-recovery and the PR
  monitor still owns the slice.
- `monitoring_pr`: PR exists and AWF owns comment/check/merge monitoring.
- `merged`: slice landed on `codex/awf-post-merge-fixes`.
- `failed`: attempt failed and needs root-cause triage or a superseding retry.
- `reschedule_required`: the previous attempt did not land; the slice remains
  in the backlog and must be retried or recovered before it can count as done.
- `superseded`: another workspace/PR completed the intended slice.

### Active Slices

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Security, Secrets, And Egress Policy | Prompt-injection boundary controls for external evidence | `ws_738700a49275436b9b96ec7e` | [#179](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/179) | monitoring_pr | Codex `gpt-5.5`; retry of no-work failed `ws_e3ca2c9f7f8d4f0181890173` after local mirror-chown fix, rebuild, and GC. |
| P1 MCP And Project Onboarding Client Parity | One-command `awf init` local bootstrap | `ws_0da5b57348cb49d198db9ee2` | [#180](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/180) | monitoring_pr | Claude Code `claude-opus-4-7`; retry of no-work failed `ws_c377ab4f6b6b452196ac7097` after local mirror-chown fix, rebuild, and GC. |
| P1 MCP And Project Onboarding Client Parity | API / CLI / MCP parity implementation driver | `ws_f4f5d0934e5f45c1ba0d7998` | pending | running | OpenCode `ollama/glm-5.1:cloud`; retry of no-work failed `ws_c6ad9ca557d441e881329388` after local mirror-chown fix, rebuild, and GC. |
| P1 Operator Console Completion | Agent and exact model workspace filters | `ws_0e5f80c0f2be464db625c766` | [#181](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/181) | monitoring_pr | Gemini `gemini-3.1-pro-preview`; retry of no-work failed `ws_a0f41e0ed1154727b9a35f16` after local mirror-chown fix, rebuild, and GC. |
| P1 Security, Secrets, And Egress Policy | Restricted egress allowlist templates | `ws_acf536de3fe3434699bee650` | pending | running | OpenCode `ollama/kimi-k2.6:cloud`; retry of no-work failed `ws_b0db737f56e64894a53ad640` after local mirror-chown fix, rebuild, and GC. |

### Reschedule Required Slices

These slices are not done. Do not count them as completed, and do not skip them
when selecting the next wave after active PR-monitor slices complete and the
local service has been pulled/rebuilt/restarted. If PR #161 merges, do not
reschedule its corresponding slice.

| TODO area | Slice | Failed workspace(s) | PR / branch | Status | Reschedule note |
| --- | --- | --- | --- | --- | --- |

### Completed Slices

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Queue fairness priority and decision records | `ws_fcee67cfbc274297b5b692df` | [#178](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/178) | merged | Codex `gpt-5.5`; recovered from the earlier conformance-stall failure, passed validation/coverage, handled comments, waited non-check reviewer settle, and merged 2026-05-02. |
| P1 Security, Secrets, And Egress Policy | Explicit workspace network postures | `ws_6c58298db1c14c8cb6a6f906` | [#177](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/177) | merged | Codex `gpt-5.5`; merged 2026-05-02; adds explicit workspace network posture support and operator surfacing. |
| P1 Control-Plane Restart Recovery Hardening | Monitoring PR restart claim recovery | `ws_50038cfe37f64e3ebb11bdae` | [#176](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/176) | merged | Codex `gpt-5.5`; merged 2026-05-02; hardens restart recovery claims for persisted `monitoring_pr` workspaces. |
| P1 Operator Console Completion | Dark theme and accessibility controls | `ws_55a1e9ab508e42038e965097` | [#175](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/175) | merged | Codex `gpt-5.5`; merged 2026-05-02; adds console dark theme and accessibility controls. |
| P1 MCP And Project Onboarding Client Parity | API / CLI / MCP parity matrix | `ws_b5a63a8954c94df5b38454f9` | [#174](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/174) | merged | Codex `gpt-5.5`; merged 2026-05-02; publishes the parity matrix, while follow-up implementation/test alignment remains tracked in the P1 checklist. |
| P0 Provider Resilience And Automated Fallback Recovery | Prevent duplicate full retry from live PR monitor | `ws_f58d575afe79430aac35ed7c` | [#173](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/173) | merged | Codex `gpt-5.5`; merged 2026-05-02 after comment handling, validation recovery, non-check reviewer settle, and auto-merge; owns the no-duplicate retry regression for live PR monitors. |
| P0 Provider Resilience And Automated Fallback Recovery | Terminal-state stale callback guard | `ws_2e6835c9c3cb4e38a0c29ec3` | [#172](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/172) | merged | Codex `gpt-5.5`; merged 2026-05-02 after validation, comment handling, non-check reviewer settle, and auto-merge; owns destroyed/destroying/completed/cancelled/failed callback authority regression. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider recovery API, metrics, merge queue, and console surfacing | `ws_ebad989fc19b41d39cb150b7` | [#171](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/171) | merged | OpenCode `ollama/glm-5.1:cloud`; merged 2026-05-02 after comment handling, validate-only and rebase-only recovery, and non-check reviewer settle. |
| P0 Provider Resilience And Automated Fallback Recovery | Conformance stall detection and recovery | `ws_52d8415a02424c4aa4730fa1` | [#169](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/169) | merged | Claude Code `claude-opus-4-7`; merged 2026-05-02 after comment handling, stale-overlap rebase recovery, validation recovery, and non-check reviewer settle. |
| P0 Provider Resilience And Automated Fallback Recovery | PR monitor provider outage recovery | `ws_2a6d2ef4186d4c48960411ec` | [#170](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/170) | merged | Gemini `gemini-3.1-pro-preview`; merged 2026-05-01 after validate-only recovery, sync-base, and non-check reviewer settle. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider fallback contract and no-loop regression coverage | `ws_bae976c92edc4a2eacc89830` | [#168](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/168) | merged | OpenCode `ollama/deepseek-v4-pro:cloud`; merged 2026-05-01 after validate-only recovery and non-check reviewer settle. |
| P0 Provider Resilience And Automated Fallback Recovery | Executor provider fallback end-to-end behavior | `ws_3434c0301e7744d6aefbd315` | [#167](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/167) | merged | Codex `gpt-5.5`; merged 2026-05-01; owns executor/service fallback attempt creation and lineage tests. |
| P0 Provider Resilience And Automated Fallback Recovery | Coverage-wrapped pytest failure classification | `ws_310bcd7bcf1949e9a8421915` | [#165](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/165) | merged | Codex `gpt-5.5`; classifies pytest failures inside coverage commands separately from true coverage-threshold failures. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider/model backoff, circuit breakers, fallback policy, and fallback attempt lineage | `ws_a012908420364984b230df51` | [#166](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/166) | merged | Codex `gpt-5.5`; landed core retry/backoff/fallback recovery loop, with follow-up commit `75704ad` fixing fallback retry counter inheritance and final-head monitor gating. |
| P0 Planning Phase Scope Enforcement | Planning-only prompt and scope failure details | `ws_42b3d10157fd4afbbbba0145` | [#164](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/164) | merged | Codex `gpt-5.5`; merged 2026-05-01 after resolving planning retry, plan artifact, and monitor recovery review comments. |
| P1 MCP And Project Onboarding Client Parity | `awf init` and smoke setup guidance | `ws_8c9f0ae88d5c477aac382158` | [#161](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/161) | merged | Reattached after fixing the validate-only recovery bug locally in `faf5911`; merged 2026-05-01. |
| P0 Provider Resilience And Automated Fallback Recovery | No-work failed idle container cleanup | `ws_cfee1e44d23a41a2aae90c8c` | [#163](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/163) | merged | Revived existing PR monitor instead of rescheduling; Codex `gpt-5.3-codex-spark`; merged 2026-05-01. |
| P1 MCP And Project Onboarding Client Parity | MCP operator parity tools | `ws_1e79f6b47faf44d0bf8de3f0` | [#159](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/159) | merged | OpenCode `ollama/glm-5.1:cloud`; retry of Gemini capacity-failed `ws_7c8ec611a3d14b6cb4612344`; merged 2026-05-01. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider-capacity failure classification | `ws_1e02f0a23ccb4cd99d2471c2` | [#162](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/162) | merged | Gemini `gemini-3.1-pro-preview`; retry after `GEMINI_API_KEY` propagation landed structured provider-capacity classification. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Queue fairness and scheduler decision records | `ws_05365f752ad742abb7c134af` | [#160](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/160) | merged | Adds scheduler decision-record planning/docs slice after the OpenCode GLM attempt stalled in conformance. |
| P1 Operator Console Completion | Security and egress status panels | `ws_ac64156e08454928985982eb` | [#158](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/158) | merged | Adds console security and egress status panels via OpenCode GLM retry. |
| P1 API Contract Completion | Guard legacy endpoint compatibility | `ws_a41728907dc740d6a1ae7092` | [#157](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/157) | merged | Guards v1/legacy response compatibility until documented v2 cutover. |
| P1 Workspace Services And Realistic Project Profiles | Strengthen DinD compose profile execution | `ws_58551268828945cfb52fe01e` | [#156](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/156) | merged | Strengthens per-workspace DinD Compose execution, health waits, cleanup, and structured failures. |
| P1 MCP And Project Onboarding Client Parity | AWF doctor diagnostics | `ws_7d33a6f9a0b24eea91058a9e` | [#155](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/155) | merged | Adds plain-language local diagnostics for Docker, API, worker, auth, provider readiness, ports, disk, stale containers, and env/config issues. |
| P1 Security, Secrets, And Egress Policy | Replace broad auth mounts with secret leases | `ws_22fc3239a5bd4d93b82ff003` | [#154](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/154) | merged | Adds declared secret lease mounts and safer local auth handling while preserving compatibility. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Agent overlap warning prompts | `ws_9d924227c4744603aeed80cf` | [#153](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/153) | merged | Includes advisory overlap graph warnings in agent prompts without blocking launch. |
| P1 Workspace Services And Realistic Project Profiles | Profile-defined app endpoints | `ws_0a911b1614e54418a2ce6877` | [#152](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/152) | merged | Adds profile app endpoint metadata for agents, validation, API, and console-safe detail. |
| P1 Workspace Services And Realistic Project Profiles | Database refresh hooks | `ws_0fdabcdbbf884682ae033426` | [#151](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/151) | merged | Adds DB-backed profile refresh/generation hooks with durable logs and structured failures. |
| P1 Workspace Services And Realistic Project Profiles | Migration-chain validation policy | `ws_f1f1f57ee64e470a9ee44821` | [#150](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/150) | merged | Adds Alembic chain validation policy for Python DB-backed profiles. |
| P1 Security, Secrets, And Egress Policy | Local secret lease lifecycle records | `ws_60b4cd270bf34577bba32d28` | [#147](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/147) | merged | Records declared secret lease issue/mount/expiry/revoke metadata and audit events. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Local resource reservation accounting | `ws_a65f1a826823480a8c7cb197` | [#146](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/146) | merged | Finishes CPU, memory, disk, and DinD reservation accounting in local scheduling/status surfaces. |
| P1 Workspace Services And Realistic Project Profiles | Redis app worker sidecar fixture | `ws_3dc7afc7f7f746f2b4e1dc44` | [#145](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/145) | merged | Adds a generic Redis/app/worker/service sidecar fixture with health and validation coverage. |
| P1 MCP And Project Onboarding Client Parity | MCP operator surface parity tools | `ws_f3005ee402404cff84ce9bb1` | [#144](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/144) | merged | Adds MCP tools and parity tests for high-value read-only operator surfaces. |
| P1 API Contract Completion | Artifact download semantics | `ws_2b64c3dbbc8943cebc184f18` | [#143](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/143) | merged | Adds safe token-protected artifact read/download semantics for AWF-managed artifact paths. |
| P1 MCP And Project Onboarding Client Parity | Project onboarding profile init and smoke guide | `ws_1f1b857f2e1d4e9db2fc6052` | [#142](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/142) | merged | Adds `docs/PROJECT_ONBOARDING.md`, profile init/preview support, templates, smoke request shape, and regression tests. |
| P1 Workspace Services And Realistic Project Profiles | Node browser workspace fixture | `ws_3a9ea5a336e04ee18849a367` | [#141](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/141) | merged | Adds a generic Node/browser validation fixture with service health and Playwright-style browser checks. |
| P1 Security, Secrets, And Egress Policy | PR/control audit event trail | `ws_9965559b81b24ee89ad6c3b3` | [#140](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/140) | merged | Adds durable structured audit events for PR creation, push/publication, merge, comment resolution, and destructive controls. |
| P1 Workspace Services And Realistic Project Profiles | Service health waits before validation | `ws_b4a9cc4b906b489ba770f9cc` | [#138](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/138) | merged | Adds profile health checks that gate validation with structured failures. |
| P1 Workspace Services And Realistic Project Profiles | Python service plus Postgres fixture | `ws_bccf2e2aa51540f38c2db104` | [#137](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/137) | merged | Adds generic DB-backed fixture/profile coverage with deterministic Docker skips. |
| P1 Security, Secrets, And Egress Policy | Provider least-privilege readiness diagnostics | `ws_e022c0672fb44022994eb276` | [#139](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/139) | merged | Exposes provider credential scope warnings without reading secret values. |
| P1 Security, Secrets, And Egress Policy | Profile lint for unsafe secret mounts | `ws_d14e2177bdc24746b552918b` | [#134](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/134) | merged | Rejects/warns unsafe secret targets and broad host-home auth mounts. |
| P1 API Contract Completion | Normalized list pagination envelopes | `ws_49f61dfb52e74dc9836aad50` | [#136](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/136) | merged | Normalizes list pagination envelope behavior while preserving backward-compatible response fields. |
| P1 Security, Secrets, And Egress Policy | Local egress policy enforcement | `ws_830a162773f845adb14caed9` | [#135](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/135) | merged | Adds local Docker-mode egress policy enforcement with tests for open/restricted modes. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Operator-visible advisory overlap graph | `ws_0bb96ca3f38142288e51ef2f` | [#133](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/133) | merged | Exposes advisory overlap visibility without blocking workspace launch. |
| P1 Local Packaging And Upgrade Path | Local backup, upgrade, and recovery runbook | `ws_f3145b63327c482aaaa37c10` | [#132](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/132) | merged | Documents local image versioning, Postgres backup/restore, rollback, disaster recovery, and `scripts/run_awf.py` compatibility. |
| P0 Operation And Recovery Truth | Safe console recovery controls | `ws_3407ebb7411448af9db52daf` | [#131](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/131) | merged | Adds safe remonitor/refresh/revalidate controls using stable recovery APIs. |
| P0 Operation And Recovery Truth | Terminal-state stale callback guard | `ws_caa5d122e46d47d1a696cf0b` | [#129](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/129) | merged | Prevents cancelled/destroyed/completed workspaces from advancing after stale callbacks. |
| P1 Operator Console Completion | PR monitor transitions visible as operations | `ws_5eac8b3526704e77ad477d0c` | [#130](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/130) | merged | Exposes monitor wait/recovery/merge transitions as durable operations and console entries. |
| P0 Test Coverage And Quality Gates | Test quality guardrails for fake tests | `ws_1aacbc2dd884497d8eab8e7d` | [#128](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/128) | merged | Forbids empty tests, fake assertions, and broad behavior-skipping monkeypatches. |
| P1 Security, Secrets, And Egress Policy | Redact known token patterns from logs | `ws_abd68626a284423594916909` | [#127](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/127) | merged | Redacts known secrets before persistence and live streaming. |
| P1 Local Packaging And Upgrade Path | Repeatable local service bootstrap | `ws_aa2d95f571fc4a128dc86f18` | [#126](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/126) | merged | Makes local service startup/migration/health bootstrap one-command and idempotent. |
| P0 Merge Safety And PR Monitor Correctness | Post-PR recovery state regression coverage | `ws_8cf441f5e78e4cb2bc646106` | [#125](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/125) | merged | Proves recovery paths do not re-enter full agent execution after PR creation. |
| P0 Operation And Recovery Truth | Active PR workspace recovery after service restart | `ws_3b51ab18f6b04912807e9197` | [#124](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/124) | merged | Resumes monitoring without rerunning agent or recreating PR. |
| P0 Reliability, Cleanup, And SLOs | Idempotent retained cleanup after Docker failures | `ws_b3507d25cb624d858f2cb9e4` | [#123](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/123) | merged | Preserves logs/artifacts and makes partial cleanup retry-safe. |
| P0 Test Coverage And Quality Gates | Dockerized sidecar workspace fixture coverage | `ws_bc51ec47dac64ea9a0077cdd` | [#122](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/122) | merged | Covers Dockerized app plus sidecar service profile behavior. |
| P0 Operation And Recovery Truth | Complete control operation idempotency matrix | `ws_b054789dccf84535b592e527` | [#121](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/121) | merged | Covers replay/conflict semantics for cancel/stop/delete/remonitor/refresh/validate/rebase. |
| P0 Validation Tier Provenance As Merge Policy | Validation tier/freshness API and console exposure | `ws_08d7037136ea44c2a6c65dff` | [#120](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/120) | merged | Exposes required/latest tier, freshness identity, and reason codes in API/console. |
| P0 Merge Safety And PR Monitor Correctness | Non-actionable bot comment regression coverage | `ws_632058449475452598e98d3e` | [#119](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/119) | merged | Proves ignored bot comments do not suppress meaningful later comments. |
| P0 Stale Detection And Merge Queue Truth | Post-merge overlap stale-risk signal | `ws_bad04ed0e59f452bb04ff4eb` | [#118](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/118) | merged | Keeps overlap advisory at launch while using post-merge overlap as stale-risk signal. |
| P0 Operation And Recovery Truth | Stranded active workspace recovery detection | `ws_173dfcb6cd3d44209923bd8e` | [#117](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/117) | merged | Detects stranded active workspaces whose runtime resources disappeared or exited. |
| P0 Test Coverage And Quality Gates | Alembic multi-head resolver integration coverage | `ws_cf269265081c44b498b713bb` | [#116](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/116) | merged | Adds resolver tests for multiple Alembic heads and generated merge revisions. |
| P0 Operation And Recovery Truth | Public idempotent rebase operation API | `ws_dc1ce9a4217e4fbcab609701` | [#115](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/115) | merged | Adds token-protected replay-safe rebase operation endpoint. |
| P0 Merge Safety And PR Monitor Correctness | Full auto-merge gate regression coverage | `ws_48757c50d7e842f896fe962f` | [#114](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/114) | merged | Covers grace/comments/checks/freshness/tier/final-settle auto-merge gates. |
| P0 Test Coverage And Quality Gates | Parallel PR stale workflow integration coverage | `ws_fbc967faf6b2488c8a9ddc3e` | [#113](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/113) | merged | Adds integration coverage for parallel PR stale/revalidate workflow. |
| P0 Validation Tier Provenance As Merge Policy | Complete validation freshness identity | `ws_b7ff7fb6515e485eb00d0f49` | [#112](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/112) | merged | Adds command/profile/environment/target identity for validation freshness. |
| P0 Operation And Recovery Truth | Explicit PR monitor recovery operation records | `ws_1a047c3867f54195b2b2ee70` | [#111](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/111) | merged | Adds durable recovery operation records for rebase/validate/remonitor/manual-wait paths. |
| P0 Stale Detection And Merge Queue Truth | Keep plan artifact overlaps advisory | `ws_4a85880fd415476f8a584079` | [#110](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/110) | merged | Prevents docs/awf-plans-only changes from hard-blocking otherwise mergeable PRs. |
| P1 Operator Console Completion | Explain workspace recovery reversals in console | `ws_8d370f44fe3c4d31b68923a8` | [#109](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/109) | merged | Console/API now explain intentional workflow step-backs such as stale PR recovery. |
| P0 Reliability, Cleanup, And SLOs | Completed PR workspace cleanup retention | `ws_d71ff14f12ac4513b45b097f` | [#108](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/108) | merged | Adds safe cleanup eligibility and retention for completed PR workspaces. |
| P0 Merge Safety And PR Monitor Correctness | Retry transient GitHub errors without losing monitor state | `ws_744d79861eb94d37b8f1e654` | [#107](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/107) | merged | PR monitor now retries transient GitHub errors without losing monitor state. |
| P0 Stale Detection And Merge Queue Truth | Dependency/build/schema stale reasons | `ws_d8629b74e7944b688ae34df6` | [#106](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/106) | merged | Adds structured stale reasons for sensitive target-branch changes. |
| P0 Operation And Recovery Truth | Mutating operation idempotency and concurrency hardening | `ws_03f3b6484d3744e1849afab8` | [#105](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/105) | merged | Hardens control API idempotency and conflict behavior. |
| P1 Operator Console Completion | Validation freshness and stale action drill-down | `ws_6d67b95a51cf46ab87aaf33e` | [#104](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/104) | merged | Console visibility for validation freshness and stale action details. |
| P0 Reliability, Cleanup, And SLOs | Orphan AWF resource detection and cleanup readiness reporting | `ws_5605c5ca71c942d999f5b78f` | [#103](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/103) | merged | Recovered through service remonitor after local stale-rebase fixes; Tier 2 validation and 99% coverage passed before auto-merge. |
| P0 Stale Detection And Merge Queue Truth | Console merge queue stale reasons and required actions | `ws_7314436b72d147949dbf681d` | [#102](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/102) | merged | UI-only slice for merge-queue clarity, using existing API fields. |
| P0 Operation And Recovery Truth | Persist operation audit details and log stream references | `ws_83f4e614951446cf883f5c09` | [#101](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/101) | merged | Feature landed even though the workspace later failed in stale-rebase recovery; the local executor now treats already-synced branches as refreshed before Tier 2 validation. |
| P0 Merge Safety And PR Monitor Correctness | Manual-merge monitor waits for observed human merge before completion | `ws_f9644d6f9c904c42ae964035` | [#100](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/100) | merged | Regression/fix slice for manual-merge lifecycle; completed after AWF observed the PR merge. |
| P0 Merge Safety And PR Monitor Correctness | Non-check async reviewer settle before auto-merge | `ws_66a5dae2ce81488ba5fc7dd1` | [#97](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/97) | merged | Adds Greptile-style reviewer quiet window when no GitHub-visible check/status exists. |
| P0 Operation And Recovery Truth | Public refresh/revalidate operation APIs | `ws_22b5ee19102b45d1a4df3337` | [#99](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/99) | merged | Adds refresh/revalidate endpoint slice; rebase endpoint remains open. |
| P0 Reliability, Cleanup, And SLOs | Fix SLO metrics DB query to avoid concurrent `AsyncSession` use | `ws_4eab2b4971de4cfd99c75b8f` | [#96](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/96) | merged | Follow-up to #93 review feedback. |
| P0 Validation Tier Provenance As Merge Policy | Enforce validation freshness in monitor merge gate | `ws_261f800d38ed4d65acb60df7` | [#95](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/95) | merged | Merge monitor now blocks on stale validation freshness. |
| P0 Test Coverage And Quality Gates | Actionable coverage gap summaries | `ws_6906bbfe6cdf451f9dd266cc` | [#94](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/94) | merged | Explains coverage shortfalls instead of only failing the threshold. |
| P0 Reliability, Cleanup, And SLOs | Recovery cleanup and stuck-state SLO metrics | `ws_3c3d5b6f539245ec84f36d2e` | [#93](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/93) | merged | Landed through GLM; later query bug fixed by #96. |
| Foundations / Planning | Detect committed plan artifacts correctly | `ws_0afdc1fdeea04b4b9d764724` | [#92](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/92) | merged | Strengthens Plan -> Execute -> Compare enforcement. |
| P0 Merge Safety And PR Monitor Correctness | Prove PR recovery stays validate-only | `ws_ccf8ef50e04e429992596cb0` | [#91](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/91) | merged | Prevents recovery from re-entering the full agent path. |
| P1 Operator Console Completion | Show exact model, lifecycle timings, and usage placeholders | `ws_d202773195704f7da08dab87` | [#90](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/90) | merged | Covers model display and lifecycle timing UI; token usage remains provider-dependent. |
| P0 Stale Detection And Merge Queue Truth | Refresh merge candidates after target reconciliation | `ws_2374dc74d81f4af0921469d7` | [#89](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/89) | merged | Target branch reconciliation now refreshes open candidate staleness. |
| P0 Merge Safety And PR Monitor Correctness | Make PR recovery grace-aware and validate-only | `ws_2a64b2c190a24e63915f519f` | [#88](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/88) | merged | Manual merge; workspace was later stopped, but PR landed. |
| P0 Operation And Recovery Truth | Keep recovery log stream metadata truthful | `ws_db808835b819446ab50dea42` | [#87](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/87) | merged | Manual merge; workspace was later stopped, but PR landed. |
| Foundations / Provider Readiness | Provider auth readiness diagnostics | `ws_ebc6de17c0414fa4bffd7fe5` | [#86](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/86) | merged | Manual merge despite workspace terminal state. |
| P0 Reliability, Cleanup, And SLOs | Terminate in-container process trees on timeout | `ws_ec22e84ff0ea4adc9c32c896` | [#85](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/85) | merged | Improves timeout cleanup behavior. |
| P0 Operation And Recovery Truth | Fail clearly when managed worktree disappears | `ws_19325816735d4d5585cb069a` | [#84](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/84) | merged | Adds clearer stranded/missing worktree failure handling. |
| P1 API Contract Completion | Dogfood failure root-cause clusters | `ws_48b6ca97ac0b4aa9aab44125` | [#83](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/83) | merged | Adds failure-analysis metrics used by the console. |

### Failed / Superseded Slices

| TODO area | Slice | Workspace | Status | Resolution |
| --- | --- | --- | --- | --- |
| P0 Validation Tier Provenance As Merge Policy | Validation freshness merge gate via Gemma | `ws_3907d2f78ba54f9293c9835c` | superseded | Failed validation; superseded by Codex workspace `ws_261f800d38ed4d65acb60df7` / PR #95. |
| P0 Reliability, Cleanup, And SLOs | Recovery cleanup and stuck-state SLO metrics via DeepSeek | `ws_15fcdd21401d4d0495747d03` | superseded | Agent failed planning artifact requirement; superseded by GLM workspace `ws_3c3d5b6f539245ec84f36d2e` / PR #93. |
| P0 Validation Tier Provenance As Merge Policy | Validation freshness merge gate via Gemma | `ws_6996faf0ee44439595403171` | superseded | Failed validation; superseded by Codex workspace `ws_261f800d38ed4d65acb60df7` / PR #95. |
| P0 Merge Safety And PR Monitor Correctness | Manual-merge monitor waits for observed human merge before completion | `ws_f1f2e2ff3de14f34ae1dcccd` | superseded | Failed profile resolution because local AWF service image was older than merged profile schema from PR #97; service was rebuilt and retried as `ws_f9644d6f9c904c42ae964035`. |
| P0 Reliability, Cleanup, And SLOs | Orphan AWF resource detection and cleanup readiness reporting | `ws_04560f5cfd914095b357cdcb` | failed | PR [#98](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/98) repeatedly hit monitor-driven stale rebase recovery; local AWF now has a rebase-recovery fix, and the slice was retried as `ws_5605c5ca71c942d999f5b78f`. |
| P0 Operation And Recovery Truth | Persist operation audit details and log stream references | `ws_83f4e614951446cf883f5c09` | failed | PR [#101](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/101) merged, so no feature retry is needed; the workspace failure exposed the stale-rebase recovery bug fixed locally before service rebuild. |
| P1 API Contract Completion | External operator callback subscriptions | `ws_0e2fc82ece7541659287e063` | failed | Agent produced local commits and passed validation/coverage, but exhausted the Plan -> Execute -> Compare iteration budget with one remaining conformance gap: event type validation was prefix-based and still allowed internal-looking namespaced event types such as `workspace.internal_secret`. Recover by redispatching a narrow callback hardening/completion slice or salvaging the preserved worktree branch. |
| P1 Security, Secrets, And Egress Policy | Prompt-injection boundary controls for external evidence | `ws_e3ca2c9f7f8d4f0181890173` | superseded | No-work infrastructure failure during provisioning: worker attempted to recursively `chown` the shared bare git mirror and failed on a host-mounted git object file before any agent ran. Fixed locally by preparing only the editable worktree for the agent user; retried as `ws_738700a49275436b9b96ec7e`. |
| P1 MCP And Project Onboarding Client Parity | One-command `awf init` local bootstrap | `ws_c377ab4f6b6b452196ac7097` | superseded | No-work mirror `chown` provisioning failure; retried as `ws_0da5b57348cb49d198db9ee2`. |
| P1 MCP And Project Onboarding Client Parity | API / CLI / MCP parity implementation driver | `ws_c6ad9ca557d441e881329388` | superseded | No-work mirror `chown` provisioning failure; retried as `ws_f4f5d0934e5f45c1ba0d7998`. |
| P1 Operator Console Completion | Agent and exact model workspace filters | `ws_a0f41e0ed1154727b9a35f16` | superseded | No-work mirror `chown` provisioning failure; retried as `ws_0e5f80c0f2be464db625c766`. |
| P1 Security, Secrets, And Egress Policy | Restricted egress allowlist templates | `ws_b0db737f56e64894a53ad640` | superseded | No-work mirror `chown` provisioning failure; retried as `ws_acf536de3fe3434699bee650`. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider-capacity failure classification via Gemini 3.1 | `ws_033d1772828042c9afa6a491` | failed | No-work Gemini auth failure: container had copied `~/.gemini` files but no Gemini/Google auth env; Gemini CLI 0.39.1 selected API-key auth and exited 41 `AGENT_AUTH_FAILED` requiring `GEMINI_API_KEY`. |
| P0 Provider Resilience And Automated Fallback Recovery | Duplicate full retry spawned from live PR monitor | `ws_46a6c903fc7c42098a63edad` | destroyed | Duplicate retry of active workspace `ws_52d8415a02424c4aa4730fa1` / PR #169 after `AGENT_IDLE_TIMEOUT`; no PR or useful work produced. Destroyed manually and tracked as urgent P0 regression under provider recovery. |
| P1 MCP And Project Onboarding Client Parity | `awf init` and smoke setup guidance via Gemini 3.1 | `ws_927647b0535242c58879f7b8` | failed | Same no-work Gemini auth failure as `ws_033d1772828042c9afa6a491`; retry only after Gemini container auth/readiness is fixed or with a different provider/model. |
| P1 MCP And Project Onboarding Client Parity | `awf init` and smoke setup guidance via Gemini 3.1 | `ws_8210d159580747f88c691ef5` | superseded | Gemini produced a useful local commit but did so during AWF's planning-only phase, touching `src/awf/cli/main.py` and `tests/unit/cli/test_init.py` before execution was allowed. AWF correctly failed the workspace for planning scope violation; retried fresh with Codex Spark as `ws_8c9f0ae88d5c477aac382158`. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Queue fairness and scheduler decision records via Gemini | `ws_19b11c564c3343c0965eee45` | superseded | Gemini service returned repeated 429 `MODEL_CAPACITY_EXHAUSTED` before any code was produced; retried with OpenCode GLM as `ws_5031649e68b34b108f23782b`. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Queue fairness and scheduler decision records via OpenCode GLM | `ws_5031649e68b34b108f23782b` | superseded | Made a local implementation commit and passed broad service/db/api validation, but stalled during conformance JSON generation after a misleading narrow-subset coverage failure; operator stopped it and restarted from scratch with Codex `gpt-5.3-codex-spark` as `ws_05365f752ad742abb7c134af`. |
| P1 MCP And Project Onboarding Client Parity | MCP operator parity tools via Gemini | `ws_7c8ec611a3d14b6cb4612344` | superseded | Gemini service returned repeated 429 `MODEL_CAPACITY_EXHAUSTED` before any code was produced; retried with OpenCode GLM as `ws_1e79f6b47faf44d0bf8de3f0`. |
| P1 Operator Console Completion | Security and egress status panels via Gemini | `ws_8a8b09feb61d4af188473bd6` | superseded | Gemini service returned repeated 429 `MODEL_CAPACITY_EXHAUSTED` before any code was produced; retried with OpenCode GLM as `ws_ac64156e08454928985982eb`. |

## Foundations Already In Place

- [x] Postgres-backed local service with API, worker, and console containers.
- [x] Docker Compose local service stack for AWF control plane.
- [x] Profile-driven workspace configuration through `.awf/workspace.yml`.
- [x] Built-in generic, Python, Node/Next.js, Docker Compose, and Aira profile concepts.
- [x] Per-workspace Docker stack launching with optional DinD support.
- [x] Agent adapters for Codex, Claude Code, Gemini, and OpenCode/Ollama.
- [x] Central agent defaults and provider readiness diagnostics.
- [x] Workspace lifecycle from request through PR creation and monitoring.
- [x] API-backed observability for workspaces, events, logs, runtime, operations, artifacts, metrics, and merge queue.
- [x] Next.js operator console with live logs and multi-workspace fullscreen log views.
- [x] PR monitor support for review comments, check gates, initial review grace, auto-merge, and manual merge waiting.
- [x] First-class task attempts, canonical merge attempts, and merge candidates.
- [x] Persisted validation run provenance and stale reason records.
- [x] Profile-enforced planning loop support: Plan -> Execute -> Compare.
- [x] AWF self-profile with 99% coverage target.

## P0: Merge Safety And PR Monitor Correctness

- [x] Ensure PR monitor never re-enters the full agent execution path for validate-only, rebase-only, or recovery-only work.
- [x] Add end-to-end regression coverage for `monitoring_pr -> ready -> running` regressions after PR creation.
- [x] Prove manual-merge mode waits until the human merge is observed, then completes and cleans up.
- [x] Prove auto-merge mode waits for grace, comments, checks, freshness, validation tier, and final settle recheck.
- [x] Prove transient GitHub errors are retried without losing monitor state.
- [x] Prove non-actionable bot comments are ignored without suppressing meaningful later comments.
- [x] Wait for configured async reviewers that do not expose GitHub checks/statuses before auto-merge.
- [x] Add explicit monitor recovery operation records for rebase, validate-only, remonitor, and human wait.
- [x] Make PR monitor state transitions visible in the console as operations, not just log text.

## P0: Stale Detection And Merge Queue Truth

- [x] Make target branch monitor detect every merged PR that can stale open candidates.
- [x] Mark candidates stale when target branch advances and validation freshness is invalid.
- [x] Treat owned-path overlap as advisory at launch time, not blocking.
- [x] Use overlap as a stale-risk input after another candidate lands.
- [x] Detect dependency/build config changes as structured stale reasons.
- [x] Detect migration/schema/model changes for migration-sensitive tasks.
- [x] Keep stale reasons active until a successful refresh/rebase plus required validation clears them.
- [x] Make `/v1/merge-queue` candidate-backed readiness the single source of truth.
- [x] Display candidate blockers, stale reasons, required action, and canonical attempt in the console.

## P0: Validation Tier Provenance As Merge Policy

- [x] Define the freshness identity for every validation run:
  command set hash, target branch, target SHA, base SHA, profile version, and environment identity.
- [x] Enforce Tier 1 as the normal profile/request validation gate.
- [x] Enforce Tier 2 after rebase, stale refresh, conflict resolution, or target branch drift.
- [x] Represent Tier 3 metadata and policy before full Tier 3 infrastructure exists.
- [x] Store validation log stream references for every validation run.
- [x] Ensure merge eligibility reads `validation_runs`, not old operation rows or log-derived state.
- [x] Expose required tier, latest satisfied tier, validation freshness, and reason code in API and console.
- [x] Prevent agents from lowering coverage thresholds, profile requirements, or PRD quality gates.

## P0: Operation And Recovery Truth

- [x] Make cancel, stop, delete, remonitor, refresh, rebase, and validate idempotent operations.
- [x] Add public API endpoints for refresh, rebase, and validate operations.
- [x] Add optimistic concurrency or equivalent conflict protection for mutating APIs.
- [x] Persist operation start, finish, owner, reason, result, failure code, and log streams.
- [x] Ensure cancelled/destroyed workspaces cannot move forward after stale executor or monitor callbacks.
- [x] Add recovery for stranded workspaces whose containers exited but DB state is active.
- [x] Add recovery for active PR workspaces after AWF service restart.
- [x] Add console controls for safe remonitor/refresh/revalidate once API semantics are stable.
- [x] Classify unsatisfied plan conformance failures with structured gaps, retry carry-forward, and salvage hints.

## P0: Planning Phase Scope Enforcement

- [x] Make the planning-stage agent prompt/system prompt unambiguous that the
  agent must create or update only the configured plan file, must not edit
  source/tests/docs outside that plan artifact, must not run implementation
  commands, and must stop after writing the plan.
- [x] Add regression tests proving the planning phase rejects out-of-scope file
  edits with a structured reason such as `AGENT_PLAN_PHASE_SCOPE_VIOLATION`,
  preserves the worktree/branch for salvage, and records an actionable retry or
  fallback recommendation.
- [x] Add an automated recovery path for planning-scope violations: either
  discard and retry planning with an approved fallback model or intentionally
  promote/salvage the preserved branch only when policy says the premature
  implementation is acceptable.

## P0: Reliability, Cleanup, And SLOs

- [x] Define and expose rolling creation success, cleanup success, stuck-state, and recovery success metrics.
- [x] Add stuck-state watchdog metrics and actionable reason codes.
- [x] Detect orphan containers, networks, volumes, and worktrees.
- [x] Automatically clean completed PR workspaces after merge and safe retention.
- [x] Preserve logs/artifacts during cleanup according to retention policy.
- [x] Make cleanup idempotent and safe after partial Docker failures.
- [x] Add SLO-style API and console indicators for local AWF health.
- [x] Keep local disk pressure and admission blocking actionable in service status.

## P0: Test Coverage And Quality Gates

- [x] Keep branch coverage enabled.
- [x] Keep AWF self-development coverage at 99%+.
- [x] Add coverage reports that explain remaining gaps instead of only failing a threshold.
- [x] Add focused tests for PR monitor recovery, stale detection, validation tier gating, and service restart recovery.
- [x] Add integration tests for two parallel PRs where one merge stales the other.
- [x] Add integration tests for Alembic multi-head detection and automatic merge revision generation.
- [x] Add integration tests for Dockerized project profiles with sidecar services.
- [x] Forbid empty tests, fake assertions, and broad monkeypatching that skips behavior under test.

## P1: Security, Secrets, And Egress Policy

- [x] Add a public AWF Core trust model document covering Docker daemon access,
  local internet egress, secrets, provider auth, GitHub credentials, untrusted
  external text, package installs, and what deterministic Core does and does
  not enforce locally. The document must also separate current AWF Core from
  future AI Operator/Architect layers above REST/CLI/MCP.
- [x] Replace broad static auth mounts with declared secret leases where possible.
- [x] Track secret lease issue, mount, expiry, revoke, and audit events.
- [x] Revoke workspace secrets when workspace reaches terminal cleanup.
- [x] Redact known token patterns from persisted logs and artifacts.
- [x] Add profile lint failures for unsafe secret targets and broad host-home mounts.
- [x] Enforce egress policy at Docker network/profile level in local mode.
- [x] Add provider-specific least-privilege credential checks for Codex, Claude, Gemini, OpenCode/Ollama, GitHub, and Docker.
- [x] Add audit trails for PR creation, push, merge, comment resolution, and destructive operations.
- [x] Add explicit workspace network posture profiles in `.awf/workspace.yml`:
  `offline`, `restricted`, and `open`. `open` should remain available for
  trusted local dogfood work such as AWF self-development, but it must be
  declared intentionally and surfaced as unrestricted internet access in API,
  MCP, console, `awf profile preview`, and `awf doctor`.
- [ ] Add reusable restricted egress allowlist templates for common local
  engineering needs: GitHub/git remotes, configured model provider APIs,
  package registries such as PyPI/npm/uv indexes, OS package mirrors when
  declared, and documentation domains. New project onboarding should recommend
  restricted mode by default and explain when to choose open mode.
- [ ] Add outbound egress audit evidence without leaking secrets: record
  workspace id, policy posture, destination host/category, allow/deny decision,
  timestamp, and reason code for policy-controlled network attempts; expose
  summary counts in service status, workspace detail, MCP, metrics, and console
  security panels.
- [ ] Add prompt-injection boundary controls for untrusted external content:
  GitHub comments, PR review text, issues, webpages, package READMEs, and CI
  logs must be passed to agents as quoted evidence with source provenance, not
  as authority over AWF/system/task policy. Add regression tests proving
  adversarial external text cannot override owned paths, validation policy,
  secret handling, merge gates, or cleanup rules.
- [ ] Add supply-chain guardrails for agent-run package installation and remote
  script execution. Profiles should be able to choose warn/block modes for
  unpinned dependency installs, curl-pipe-shell patterns, unexpected registry
  hosts, and lockfile changes outside owned paths; violations should produce
  structured findings and operator-visible recovery guidance.

## P1: Workspace Services And Realistic Project Profiles

- [x] Strengthen Docker Compose profile execution inside per-workspace DinD.
- [x] Add integration fixtures for Python service plus Postgres.
- [x] Add integration fixtures for Node/Next.js plus browser/Playwright validation.
- [x] Add Redis/app/worker/service sidecar examples.
- [x] Add health-check wait semantics before validation.
- [x] Add profile-defined app endpoints exposed to agents and validation commands.
- [x] Add database refresh/generation hooks for DB-backed profiles.
- [x] Add migration-chain validation for Python/Alembic workloads.

## P1: Scheduler, Reservations, And Advisory Overlap Graph

- [x] Keep workspace/task submission non-blocking when owned paths overlap.
- [x] Add an operator-visible overlap graph for running and queued workspaces.
- [x] Use overlap graph to warn agents in prompts and stale policy, not to prevent parallel work.
- [x] Finish resource reservation accounting for CPU, memory, disk, and DinD pressure.
- [x] Add fairness and starvation prevention for long-lived queues.
- [x] Add task class bias and priority scoring as described in the PRD.
- [x] Add human-escalation boost and retry-aware queue scoring.
- [x] Make scheduler decisions visible as durable records and console explanations.

## P0: Provider Resilience And Automated Fallback Recovery

- [x] **Implement full provider/model automatic recovery loop** as one
  comprehensive P0 slice. Recent incidents: Gemini 429/capacity failures,
  Gemini auth failures, Codex Spark usage-limit failures in
  `ws_6c5890fe7d2b43b4ba94c8ad` and `ws_0d9b0d2e6b1d48149c0c5291`, and PR
  monitor recovery loops repeatedly hammering an exhausted model. Acceptance:
  AWF detects provider/model auth, quota, capacity, usage-limit, timeout, and
  no-work failures; stores structured reason codes, stderr fingerprints, retry
  eligibility, retry-after/cooldown, and recommended next action; places
  retryable no-work failures into delayed retry/backoff instead of terminal
  generic `agent_failure`; supports per-workspace approved fallback policy at
  creation time; creates superseding fallback attempts while preserving
  task/attempt/canonical-attempt lineage, owned paths, validation policy,
  auto-merge, review grace, PR monitor policy, and prompt/plan artifacts;
  applies provider/model circuit breakers so one exhausted model does not get
  repeatedly selected by scheduler or PR monitor recovery; cleans idle no-work
  containers only after logs/artifacts/salvage metadata are retained; exposes
  recovery state in API, events, operations, merge queue, metrics, and console;
  and proves with TDD that transient provider failures recover automatically
  while deterministic agent failures do not loop forever.
- [x] Detect no-output or over-duration stalls in Plan -> Execute -> Compare
  subphases, especially conformance/report generation, and classify them with
  structured reason codes such as `AGENT_STALLED_IN_CONFORMANCE` instead of
  leaving the workspace indefinitely `running` or collapsing it into generic
  `agent_failure`.
- [x] Recover stalled conformance attempts by preserving the worktree, local
  commits, validation logs, and saved plan; then either retry only the
  conformance/report phase with an approved fallback model or proceed to
  validation when the implementation is complete and the missing artifact is
  limited to the conformance JSON.
- [x] Detect provider-capacity and quota markers from agent CLIs, including
  `RESOURCE_EXHAUSTED`, `MODEL_CAPACITY_EXHAUSTED`, `RetryableQuotaError`,
  provider HTTP 429s, and equivalent OpenCode/Ollama, Codex, Claude, and
  Gemini transient capacity errors.
- [x] Store structured provider failure reason codes such as
  `AGENT_PROVIDER_CAPACITY_EXHAUSTED` instead of collapsing retryable provider
  outages into generic `agent_failure`.
- [x] Add delayed retry/backoff for no-work provider failures, preserving
  task/attempt lineage and making retry state visible in operations, events,
  API responses, merge queue context, and console surfaces.
- [x] Prevent provider-recovery retries from spawning a duplicate full feature
  workspace when the source workspace is already `monitoring_pr` with an open
  PR and an active monitor can continue. Regression case:
  `ws_52d8415a02424c4aa4730fa1` hit `AGENT_IDLE_TIMEOUT` while monitoring
  PR #169, but AWF created duplicate retry workspace
  `ws_46a6c903fc7c42098a63edad`. Recovery must retry or fall back the monitor
  in place, or attach a monitor-only fallback to the existing PR/branch, unless
  the source workspace is terminal or explicitly abandoned.
- [x] Block stale executor/provider-recovery callbacks from moving a workspace
  out of `destroyed`, `destroying`, `completed`, `cancelled`, or `failed`.
  Regression case: after operator destroy removed duplicate workspace
  `ws_46a6c903fc7c42098a63edad`, an in-flight executor callback observed the
  removed worktree and moved the record from `destroyed` to `validating` and
  then `failed`. Destroy/cancel/terminal state must remain authoritative, with
  stale callbacks recorded as ignored audit events.
- [x] Classify pytest failures inside coverage commands separately from true
  coverage failures when coverage meets the configured threshold, including
  failing test node IDs, coverage percent/threshold, exit status, and focused
  retry guidance in API, console, and retry prompts.
- [x] Add provider/model circuit breakers that pause new dispatches to a
  failing provider/model after repeated transient capacity failures, with
  configurable cooldown windows and operator-visible reason codes.
- [x] Add per-workspace fallback policy at creation time so a task can declare
  approved fallback providers/models, for example Gemini -> OpenCode GLM or
  Gemini -> Codex `gpt-5.5`, while preserving canonical task/attempt lineage
  and recording why the fallback was selected.
- [x] Clean up no-work failed containers, networks, and pressure directories
  after logs/artifacts are durably retained, without removing evidence needed
  for failure analysis or retries.
- [x] Add TDD coverage proving provider-capacity failures retry or fallback
  automatically, non-transient agent failures do not loop forever, and fallback
  attempts inherit validation, owned paths, profile, auto-merge, and monitor
  policy correctly.

## P1: Control-Plane Restart Recovery Hardening

- [x] When a restarted worker recovers a persisted `monitoring_pr` workspace,
  clear or expire irrelevant stale execution claims from the previous worker,
  preserve the active monitor claim, emit an explicit recovery event, and prove
  with regression tests that PR monitoring continues without duplicate monitor
  loops or misleading execution-capacity reservations.

## P1: API Contract Completion

- [x] Normalize pagination envelopes across list APIs.
- [x] Add explicit idempotency support to every mutating endpoint.
- [x] Add optimistic concurrency or version checks to mutating workspace/candidate operations.
- [x] Add callbacks/webhook support for external operators.
- [x] Add first-class operation endpoints for rebase, validate, refresh, and make-canonical.
- [x] Add artifact listing and download semantics beyond metadata.
- [x] Add failure analysis API with root cause, evidence links, and suggested recovery actions.
- [x] Keep old compatibility endpoints stable until a documented v2 API cutover.

## P1: MCP And Project Onboarding Client Parity

Decision: keep the REST API as the canonical AWF control-plane contract, keep the
CLI as operator convenience over that API, and make MCP a first-class parity
client for agent orchestrators. Project onboarding should be a repeatable AWF
workflow, not a one-off LLM guess: an agent-facing guide plus profile templates
and a CLI-assisted inspect/preview/smoke path. New-computer setup should be
absurdly easy: install AWF, run `awf init`, start the local service, then ask a
coding agent in any project to use AWF for a feature.

- [x] Add an executable local AWF Core release scorecard as
  `awf service readiness --format json`, aggregating service readiness, doctor
  diagnostics, provider readiness, cleanup/orphan posture, failure analysis, and
  in-repo demo smoke evidence.
- [x] Add PRD SLO thresholds to the Core release scorecard: workspace creation
  success, cleanup success, stuck-state rate, and actionable failure reason
  coverage must meet the local release bar or be explicitly allowlisted with a
  written rationale.
- [x] Reuse collected service readiness status inside the scorecard doctor path
  so expensive Docker/provider probes are not double-run, while preserving
  standalone `awf service doctor` independence.
- [x] Make the release scorecard fail on unknown or generic recent workspace
  failure reasons such as `unknown` and `agent_failure`, unless explicitly
  allowlisted with release rationale.
- [x] Add a maintained in-repo golden-path demo project under
  `examples/awf-core-demo` so onboarding/profile preview and smoke-request
  generation are exercised against a realistic Python/Postgres project shape.
- [x] Add an executable offline golden-path smoke for `examples/awf-core-demo`
  covering profile preview, generated workspace request, local validation
  evidence, mocked-local PR monitor evidence, and mocked-local cleanup evidence.
- [x] Expose the Core release scorecard through REST and MCP parity surfaces:
  `GET /release-readiness`, `awf service readiness --format json`, and
  `awf_get_core_release_readiness`.
- [x] Document the AWF Core release gate as the local open-source readiness bar
  before GKE discussion.
- [x] Add open-source hygiene files for local Core: `SECURITY.md`,
  `CONTRIBUTING.md`, GitHub issue templates, and a PR template with local/BYOK
  support boundary notes.
- [x] Add CI gates for console lint/typecheck/build/browser smoke and release
  artifact/image validation for wheel/sdist, CLI entrypoints, control-plane
  image, and agent-runtime image.
- [ ] Define the primary install path: package-manager install such as `uv tool install aira-awf`/`uv pip install aira-awf`, with git clone as the contributor path.
- [ ] Add a one-command local bootstrap such as `awf init` that checks Docker, writes local env defaults, creates the AWF state directory, starts or validates Postgres/API/worker/console, and prints next steps.
- [x] Add `awf doctor` or extend `awf service status` to diagnose missing Docker, auth, API token, GitHub CLI, provider credentials, ports, disk, and stale containers in plain language.
- [ ] Add copy-paste onboarding prompts for Codex, Claude Code, Gemini, OpenCode, and OpenClaw: "inspect this project, generate `.awf/workspace.yml`, preview it, launch a smoke workspace, then implement feature X through AWF."
- [ ] Add a smoke workspace command that can be run from any project after `awf init` to prove the local service, auth, profile, validation, PR creation, and console links work.
- [x] Publish an API/CLI/MCP parity matrix and treat missing MCP coverage as an explicit backlog item.
- [ ] Convert the parity matrix into an implementation driver: every surface marked
  missing or partial must map to a concrete P1 implementation issue/slice, with
  REST endpoint, CLI command, MCP tool name, schema/error-code contract, and
  security boundary recorded. The matrix should not be considered complete if it
  only documents gaps without creating executable follow-up work.
- [ ] Add MCP tools for merge queue, task attempts, validation provenance, stale reasons, artifacts, metrics, locks/overlap graph, and service health/status.
- [ ] Add MCP tools for safe operator actions already present in the API: remonitor, refresh, validate, rebase, retry, cancel, stop, and destroy, with the same idempotency/concurrency semantics.
- [ ] Align CLI command coverage with the canonical REST API and MCP surfaces:
  for each safe read/control operation, either expose the corresponding CLI
  command with the same auth/idempotency/concurrency/error semantics, or document
  why that surface is intentionally MCP/API-only.
- [ ] Keep MCP read/control scoped: expose AWF-managed runtime snapshots, logs, operations, and controls, but do not expose arbitrary shell or unrestricted Docker exec.
- [ ] Add contract tests proving REST API, CLI, and MCP stay aligned: request
  payloads, response payloads, reason codes, idempotency keys, `If-Match` /
  workspace-version concurrency, auth failures, and structured error semantics
  must not drift across the three clients.
- [ ] Add a docs/status consistency test for the parity matrix so entries marked
  implemented must correspond to real REST routes, CLI commands, MCP tools, and
  contract-test coverage; partial or missing entries must remain visible as
  unchecked backlog work.
- [x] Add `docs/PROJECT_ONBOARDING.md` for Codex, Claude Code, Gemini, OpenCode, OpenClaw, and human operators.
- [x] Add `awf project init` or `awf profile init` to inspect a repository and generate a draft `.awf/workspace.yml`.
- [x] Add profile templates for common project shapes: generic, Python, Node/Next.js, Docker Compose, Python+Postgres, Node+browser/Playwright, and multi-service app.
- [x] Make onboarding run `awf profile preview` automatically and report missing services, secrets, ports, validation commands, and health checks.
- [x] Add an optional smoke-workspace generator that creates a tiny no-op/check-only AWF workspace request from the generated profile.
- [x] Add regression tests for onboarding detection, generated profile validity, preview output, and smoke request shape.

## P1: Developer Experience And Public Core Surface

Decision: treat local AWF Core as an open-source developer product, not only an
operator-controlled internal system. A new evaluator should be able to install
AWF, run one proof, understand failures, and discover API/CLI/MCP contracts
without reading the whole repo.

- [ ] Add a top-level "Start Here" quickstart that gets a fresh evaluator from
  clone/install to meaningful AWF proof in under five minutes. Acceptance:
  three commands or fewer for the recommended path, expected output snippets,
  prerequisites called out before the first command, and links to deeper docs
  only after the first successful proof.
- [ ] Split the README into a short product landing plus focused docs:
  getting started, concepts, CLI reference, REST API reference, MCP reference,
  troubleshooting, trust model, and contributor guide. Acceptance: README
  remains scannable under roughly 300 lines and points to one canonical doc for
  each developer journey stage.
- [ ] Add an executable first-run DX smoke command, such as `awf demo run` or
  `awf smoke run`, that prints a step-by-step report for service readiness,
  profile preview, workspace request, validation evidence, PR/monitor evidence
  or mocked-local equivalent, and cleanup evidence. Acceptance: it is safe to
  run repeatedly, works without live GitHub when using mocked-local mode, and
  produces clear next actions on failure.
- [ ] Publish a stable OpenAPI artifact and API examples for the Core control
  plane. Acceptance: `openapi.json` can be generated in CI, linked from docs,
  and paired with copy-paste `curl` examples for create workspace, list status,
  read logs/events, request validation, remonitor, retry, and release
  readiness.
- [ ] Decide the SDK stance before open-source Core release: either ship a
  minimal Python client for the stable operator flows or explicitly document
  that REST + CLI + MCP are the supported client surfaces for v0.1. Acceptance:
  the decision is reflected in README, API docs, and the parity matrix so
  integrators do not write against accidental internal modules.
- [ ] Add a searchable reason-code and error-code catalog. Acceptance: common
  API/CLI/MCP failures include problem, likely cause, operator fix, related
  command, and docs link; release readiness fails if new public reason codes
  lack catalog coverage.
- [ ] Improve CLI help text for first-time users. Acceptance: `awf --help`,
  `awf init --help`, `awf service bootstrap --help`, and workspace commands
  explain the recommended first path, safety defaults, dry-run behavior, and
  whether the command mutates local state, Docker, GitHub, or Git branches.
- [ ] Add a troubleshooting guide organized by first-run failure symptom:
  Docker unavailable, Postgres unavailable, GitHub auth missing, provider auth
  missing, package install failure, disk pressure, port conflict, provider
  outage, stale PR monitor, and cleanup/orphan warning. Acceptance: every item
  includes the exact command to diagnose and the safest recovery command.
- [ ] Add docs search/readability checks for public docs. Acceptance: CI or a
  docs-status test confirms every public guide is linked from the docs index,
  key commands still exist in CLI help, and snippets marked copy-paste are
  syntactically valid.
- [ ] Add a "first-time evaluator" telemetry-free feedback loop for local Core,
  such as a generated `awf doctor --bundle` redacted support artifact or a
  GitHub issue template path from failed readiness output. Acceptance: no
  secrets are included, and developers can file useful issues without manually
  collecting ten logs.

## P1: Operator Console Completion

- [x] Show exact agent model and thinking/effort settings for every workspace.
- [ ] Add console filters for all supported agents, including OpenCode, and an
  exact model filter so operators can view workspaces by provider/model while
  defaulting to all workspaces.
- [x] Show lifecycle stage start time, end time, and duration.
- [x] Show validation tier, validation freshness, command hash, and target SHA.
- [ ] Show token usage when providers expose it.
- [ ] Show estimated cost only when reliable pricing metadata is configured.
- [x] Add merge queue blocker drill-down.
- [x] Add stale reason and recovery action drill-down.
- [x] Add safe remonitor/refresh/revalidate controls after API hardening.
- [ ] Add live workspace activity signals such as `last_activity_at`,
  `last_log_at`, active agent/conformance/validation subphase, and stale-running
  warnings so operators can distinguish a genuinely working agent from a
  stuck `running` workspace whose row `updated_at` has not changed.
- [x] Add security/secret/egress status panels.
- [x] Add a polished dark theme and accessibility controls for the web console,
  including larger font-size options, high-contrast mode, preserved operator
  preference, keyboard/focus-visible coverage, and browser-verified responsive
  screenshots for the main dashboard, workspace inspector, logs, and merge
  queue.
- [ ] Restructure the wide-screen console so global dashboard panes stay
  stable, while workspace-specific panes open in a dismissible embedded
  inspector that can be closed to reset the selected workspace.

## P1: Local Packaging And Upgrade Path

- [x] Make local service bootstrap one-command and repeatable.
- [x] Make migrations run safely during service startup or documented bootstrap.
- [x] Add image versioning and local upgrade notes.
- [x] Add release-readiness CI checks that build/install wheel and sdist,
  verify CLI entrypoints, and validate control-plane plus agent-runtime Docker
  image builds before public release.
- [x] Add backup/restore instructions for AWF control-plane Postgres.
- [x] Add local disaster recovery instructions for stuck containers, broken migrations, and corrupt work dirs.
- [x] Keep `scripts/run_awf.py` compatibility documented until the API-backed runner fully replaces it.

## P2: GKE Readiness Design

Do not begin implementation here until P0 and P1 are complete. Deferring a P1
requires an explicit ledger note explaining why it is not needed for a superb
local open-source AWF Core.

- [ ] Define GKE control-plane deployment topology.
- [ ] Define worker/node-agent split for Kubernetes.
- [ ] Replace local Docker Compose workspace launcher with Kubernetes Jobs/Pods where appropriate.
- [ ] Define PVC/cache/worktree/mirror strategy.
- [ ] Define image registry and runtime image pinning.
- [ ] Define Workload Identity and GitHub credential strategy.
- [ ] Define Kubernetes NetworkPolicy for workspace egress.
- [ ] Define autoscaling, quota, and cost controls.
- [ ] Define Helm or Kustomize deployment package.
- [ ] Define production logging, metrics, traces, and alerting.

## Ready For GKE Discussion When

- [ ] All P0 items are complete, including active AWF dogfood slices and the
  umbrella provider-recovery acceptance item.
- [ ] All P1 items are complete, or explicitly deferred with a written reason
  that preserves the local open-source Core readiness bar.
- [ ] `awf service readiness --format json` passes without generic recent
  failure reasons, with PRD SLO thresholds met over the rolling window; any
  temporary allowlist has written release rationale.
- [ ] PR monitor and merge queue can safely handle many parallel PRs with overlap.
- [ ] Stale detection and validation tier policy are enforced as merge blockers.
- [ ] Recovery operations are idempotent, observable, and restart-safe.
- [ ] Cleanup is reliable and measured.
- [ ] Secret and egress policy has real enforcement, not just schema, including
  explicit network posture, restricted allowlists, egress audit evidence,
  prompt-injection boundaries, and supply-chain guardrails.
- [ ] Provider/model capacity failures are classified, retried or routed through
  approved fallback policy, and cleaned up without manual intervention.
- [ ] The public AWF Core trust model is current for Docker, egress, secrets,
  provider auth, GitHub credentials, untrusted text, and package installs.
- [ ] The console can explain every blocked workspace without reading raw logs.
- [ ] AWF self-development passes 99%+ coverage with meaningful tests.
- [ ] The maintained `examples/awf-core-demo` golden path proves onboarding,
  profile preview, smoke request generation, workspace lifecycle, validation,
  PR monitor path, and cleanup end to end.
- [ ] A first-time evaluator can install or clone AWF Core, run the recommended
  quickstart, see a meaningful release/demo proof, and understand failures in
  under five minutes without reading the full README.
- [ ] Public API/CLI/MCP docs and the SDK/no-SDK stance are explicit enough that
  external integrators know which surfaces are stable and which internals are
  unsupported.
