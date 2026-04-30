# AWF Pre-GKE Industrial Readiness Checklist

Last updated: 2026-04-30

This checklist is the standing plan for moving AWF from a strong local
agent-workspace fabric into a robust industrial system that is ready for a
GKE deployment design. It is based on the current codebase compared against
`docs/awf_prd_v2.2.md`.

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
- Treat unchecked P0/P1 items as blockers before serious GKE work.
- Treat the Active / Completed Slices ledger as the durable anti-duplication
  record for AWF dogfood tasks. Do not launch a new workspace for a slice that
  is already `running`, `monitoring_pr`, or `merged`.
- Update this file whenever a PR lands that materially completes an item.
- Prefer AWF dogfood delivery with `auto_merge=true` for non-`main` targets.
- Keep TDD mandatory and keep AWF self-development coverage at 99%+.

Priority key:

- P0: required before GKE design starts.
- P1: required before a credible GKE pilot.
- P2: can be planned during or after the first GKE pilot.

## Active / Completed AWF Slices

Status values:

- `running`: workspace is actively implementing the slice.
- `monitoring_pr`: PR exists and AWF owns comment/check/merge monitoring.
- `merged`: slice landed on `codex/awf-post-merge-fixes`.
- `failed`: attempt failed and needs root-cause triage or a superseding retry.
- `superseded`: another workspace/PR completed the intended slice.

### Active Slices

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Provider Resilience And Automated Fallback Recovery | Provider-capacity failure classification | `ws_1e02f0a23ccb4cd99d2471c2` | - | running | Gemini `gemini-3.1-pro-preview`; retry after `GEMINI_API_KEY` was propagated to AWF API/worker service env. |
| P1 MCP And Project Onboarding Client Parity | `awf init` and smoke setup guidance | `ws_8210d159580747f88c691ef5` | - | running | Gemini `gemini-3.1-pro-preview`; retry after `GEMINI_API_KEY` was propagated to AWF API/worker service env. |
| P1 Workspace Services And Realistic Project Profiles | Strengthen DinD compose profile execution | `ws_58551268828945cfb52fe01e` | [#156](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/156) | monitoring_pr | Gemini `gemini-3-pro-preview`; focused on per-workspace DinD Compose execution, health waits, cleanup, and structured failures. |
| P1 API Contract Completion | Guard legacy endpoint compatibility | `ws_a41728907dc740d6a1ae7092` | [#157](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/157) | monitoring_pr | Gemini `gemini-3-pro-preview`; focused on v1/legacy response compatibility until documented v2 cutover. |
| P1 Scheduler, Reservations, And Advisory Overlap Graph | Queue fairness and scheduler decision records | `ws_05365f752ad742abb7c134af` | [#160](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/160) | monitoring_pr | Codex `gpt-5.3-codex-spark`; fresh restart after OpenCode GLM stalled in conformance. |
| P1 MCP And Project Onboarding Client Parity | MCP operator parity tools | `ws_1e79f6b47faf44d0bf8de3f0` | [#159](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/159) | monitoring_pr | OpenCode `ollama/glm-5.1:cloud`; retry of Gemini capacity-failed `ws_7c8ec611a3d14b6cb4612344`. |
| P1 Operator Console Completion | Security and egress status panels | `ws_ac64156e08454928985982eb` | [#158](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/158) | monitoring_pr | OpenCode `ollama/glm-5.1:cloud`; retry of Gemini capacity-failed `ws_8a8b09feb61d4af188473bd6`. |

### Completed Slices

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
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
| P1 Provider Resilience And Automated Fallback Recovery | Provider-capacity failure classification via Gemini 3.1 | `ws_033d1772828042c9afa6a491` | failed | No-work Gemini auth failure: container had copied `~/.gemini` files but no Gemini/Google auth env; Gemini CLI 0.39.1 selected API-key auth and exited 41 `AGENT_AUTH_FAILED` requiring `GEMINI_API_KEY`. |
| P1 MCP And Project Onboarding Client Parity | `awf init` and smoke setup guidance via Gemini 3.1 | `ws_927647b0535242c58879f7b8` | failed | Same no-work Gemini auth failure as `ws_033d1772828042c9afa6a491`; retry only after Gemini container auth/readiness is fixed or with a different provider/model. |
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

- [x] Replace broad static auth mounts with declared secret leases where possible.
- [x] Track secret lease issue, mount, expiry, revoke, and audit events.
- [x] Revoke workspace secrets when workspace reaches terminal cleanup.
- [x] Redact known token patterns from persisted logs and artifacts.
- [x] Add profile lint failures for unsafe secret targets and broad host-home mounts.
- [x] Enforce egress policy at Docker network/profile level in local mode.
- [x] Add provider-specific least-privilege credential checks for Codex, Claude, Gemini, OpenCode/Ollama, GitHub, and Docker.
- [x] Add audit trails for PR creation, push, merge, comment resolution, and destructive operations.

## P1: Workspace Services And Realistic Project Profiles

- [ ] Strengthen Docker Compose profile execution inside per-workspace DinD.
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
- [ ] Add fairness and starvation prevention for long-lived queues.
- [ ] Add task class bias and priority scoring as described in the PRD.
- [ ] Add human-escalation boost and retry-aware queue scoring.
- [ ] Make scheduler decisions visible as durable records and console explanations.

## P1: Provider Resilience And Automated Fallback Recovery

- [ ] Detect no-output or over-duration stalls in Plan -> Execute -> Compare
  subphases, especially conformance/report generation, and classify them with
  structured reason codes such as `AGENT_STALLED_IN_CONFORMANCE` instead of
  leaving the workspace indefinitely `running` or collapsing it into generic
  `agent_failure`.
- [ ] Recover stalled conformance attempts by preserving the worktree, local
  commits, validation logs, and saved plan; then either retry only the
  conformance/report phase with an approved fallback model or proceed to
  validation when the implementation is complete and the missing artifact is
  limited to the conformance JSON.
- [ ] Detect provider-capacity and quota markers from agent CLIs, including
  `RESOURCE_EXHAUSTED`, `MODEL_CAPACITY_EXHAUSTED`, `RetryableQuotaError`,
  provider HTTP 429s, and equivalent OpenCode/Ollama, Codex, Claude, and
  Gemini transient capacity errors.
- [ ] Store structured provider failure reason codes such as
  `AGENT_PROVIDER_CAPACITY_EXHAUSTED` instead of collapsing retryable provider
  outages into generic `agent_failure`.
- [ ] Add delayed retry/backoff for no-work provider failures, preserving
  task/attempt lineage and making retry state visible in operations, events,
  API responses, merge queue context, and console surfaces.
- [ ] Add provider/model circuit breakers that pause new dispatches to a
  failing provider/model after repeated transient capacity failures, with
  configurable cooldown windows and operator-visible reason codes.
- [ ] Add per-workspace fallback policy at creation time so a task can declare
  approved fallback providers/models, for example Gemini -> OpenCode GLM or
  Gemini -> Codex `gpt-5.5`, while preserving canonical task/attempt lineage
  and recording why the fallback was selected.
- [ ] Clean up no-work failed containers, networks, and pressure directories
  after logs/artifacts are durably retained, without removing evidence needed
  for failure analysis or retries.
- [ ] Add TDD coverage proving provider-capacity failures retry or fallback
  automatically, non-transient agent failures do not loop forever, and fallback
  attempts inherit validation, owned paths, profile, auto-merge, and monitor
  policy correctly.

## P1: Control-Plane Restart Recovery Hardening

- [ ] When a restarted worker recovers a persisted `monitoring_pr` workspace,
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
- [ ] Keep old compatibility endpoints stable until a documented v2 API cutover.

## P1: MCP And Project Onboarding Client Parity

Decision: keep the REST API as the canonical AWF control-plane contract, keep the
CLI as operator convenience over that API, and make MCP a first-class parity
client for agent orchestrators. Project onboarding should be a repeatable AWF
workflow, not a one-off LLM guess: an agent-facing guide plus profile templates
and a CLI-assisted inspect/preview/smoke path. New-computer setup should be
absurdly easy: install AWF, run `awf init`, start the local service, then ask a
coding agent in any project to use AWF for a feature.

- [ ] Define the primary install path: package-manager install such as `uv tool install aira-awf`/`uv pip install aira-awf`, with git clone as the contributor path.
- [ ] Add a one-command local bootstrap such as `awf init` that checks Docker, writes local env defaults, creates the AWF state directory, starts or validates Postgres/API/worker/console, and prints next steps.
- [x] Add `awf doctor` or extend `awf service status` to diagnose missing Docker, auth, API token, GitHub CLI, provider credentials, ports, disk, and stale containers in plain language.
- [ ] Add copy-paste onboarding prompts for Codex, Claude Code, Gemini, OpenCode, and OpenClaw: "inspect this project, generate `.awf/workspace.yml`, preview it, launch a smoke workspace, then implement feature X through AWF."
- [ ] Add a smoke workspace command that can be run from any project after `awf init` to prove the local service, auth, profile, validation, PR creation, and console links work.
- [ ] Publish an API/CLI/MCP parity matrix and treat missing MCP coverage as an explicit backlog item.
- [ ] Add MCP tools for merge queue, task attempts, validation provenance, stale reasons, artifacts, metrics, locks/overlap graph, and service health/status.
- [ ] Add MCP tools for safe operator actions already present in the API: remonitor, refresh, validate, rebase, retry, cancel, stop, and destroy, with the same idempotency/concurrency semantics.
- [ ] Keep MCP read/control scoped: expose AWF-managed runtime snapshots, logs, operations, and controls, but do not expose arbitrary shell or unrestricted Docker exec.
- [ ] Add contract tests proving MCP tool payloads stay aligned with the corresponding REST API schemas and reason codes.
- [x] Add `docs/PROJECT_ONBOARDING.md` for Codex, Claude Code, Gemini, OpenCode, OpenClaw, and human operators.
- [x] Add `awf project init` or `awf profile init` to inspect a repository and generate a draft `.awf/workspace.yml`.
- [x] Add profile templates for common project shapes: generic, Python, Node/Next.js, Docker Compose, Python+Postgres, Node+browser/Playwright, and multi-service app.
- [x] Make onboarding run `awf profile preview` automatically and report missing services, secrets, ports, validation commands, and health checks.
- [x] Add an optional smoke-workspace generator that creates a tiny no-op/check-only AWF workspace request from the generated profile.
- [x] Add regression tests for onboarding detection, generated profile validity, preview output, and smoke request shape.

## P1: Operator Console Completion

- [x] Show exact agent model and thinking/effort settings for every workspace.
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
- [ ] Add security/secret/egress status panels.
- [ ] Restructure the wide-screen console so global dashboard panes stay
  stable, while workspace-specific panes open in a dismissible embedded
  inspector that can be closed to reset the selected workspace.

## P1: Local Packaging And Upgrade Path

- [x] Make local service bootstrap one-command and repeatable.
- [x] Make migrations run safely during service startup or documented bootstrap.
- [x] Add image versioning and local upgrade notes.
- [x] Add backup/restore instructions for AWF control-plane Postgres.
- [x] Add local disaster recovery instructions for stuck containers, broken migrations, and corrupt work dirs.
- [x] Keep `scripts/run_awf.py` compatibility documented until the API-backed runner fully replaces it.

## P2: GKE Readiness Design

Do not begin implementation here until P0 is complete and most P1 items are either
complete or consciously deferred.

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

- [ ] PR monitor and merge queue can safely handle many parallel PRs with overlap.
- [ ] Stale detection and validation tier policy are enforced as merge blockers.
- [ ] Recovery operations are idempotent, observable, and restart-safe.
- [ ] Cleanup is reliable and measured.
- [ ] Secret and egress policy has real enforcement, not just schema.
- [ ] Provider/model capacity failures are classified, retried or routed through
  approved fallback policy, and cleaned up without manual intervention.
- [ ] The console can explain every blocked workspace without reading raw logs.
- [ ] AWF self-development passes 99%+ coverage with meaningful tests.
- [ ] A Dockerized toy project with DB, app, and browser validation passes end to end.
