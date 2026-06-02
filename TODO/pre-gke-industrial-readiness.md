# AWF Pre-GKE Industrial Readiness Checklist

Last updated: 2026-05-19

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

Active slices are currently recorded below. The previous active PRs `#242`, `#243`,
`#245`, and `#246` were reconciled as merged during the 2026-05-14 readiness pass.

| TODO area | Slice | Workspace | Agent / model | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| _none_ | _none_ | _none_ | _none_ | _none_ | Live AWF state showed zero active workspaces on 2026-05-21 after PRs [#264](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/264), [#268](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/268), and [#272](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/272) merged. |

### Reschedule Required Slices

These slices are not done. Do not count them as completed, and do not skip them
when selecting the next wave after active PR-monitor slices complete and the
local service has been pulled/rebuilt/restarted. The `awf init` / smoke guidance
slice from PR #161 has merged and is recorded under Completed Slices, so it is
not listed here.

| TODO area | Slice | Workspace | Agent / model | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| _none_ | _none_ | _none_ | _none_ | _none_ | The 2026-05-20 recovery-required PRs were re-adopted as fresh monitors and have since completed: `ws_986a024640994f17a0f39897` / PR [#264](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/264), `ws_0367e5e1266d4acdbd13441a` / PR [#268](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/268), and `ws_53702c4210de4ec59e9ec059` / PR [#272](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/272). No reschedule-required P0/P1 slices remain as of 2026-05-21. |

### Pending Capacity Slices

These slices are ready to launch, but are not represented by an AWF workspace
yet because the current local Core policy caps active work at five workspaces.
Do not create a sixth `requested` workspace: `requested` is the workspace
queue state and still counts as active work. Launch the top pending slice when
an active slot opens.

| TODO area | Slice | Workspace | Agent / model | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| _none_ | _none_ | _none_ | _none_ | _none_ | No P0/P1 slice is currently waiting only for capacity as of 2026-05-21; live AWF state showed zero active workspaces. |

Historical failed attempts are kept under Failed / Superseded Slices for
root-cause history.

### Completed Slices

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Local Service Readiness | Align `awf init` Compose env file behavior | `ws_986a024640994f17a0f39897` | [#264](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/264) | merged | Merged 2026-05-20. Fresh monitor replaced cancelled stale monitor `ws_6b8303d60f8949d78b1237e7` after rollback of the mistaken `plans/*` ignore, branch repair, AWF rebuild, and cleanup of terminal orphan resources. |
| P1 Test Coverage And Quality Gates | Make protected quality-gate guardrail diff-aware | `ws_0367e5e1266d4acdbd13441a` | [#268](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/268) | merged | Merged 2026-05-20. Fresh monitor replaced cancelled stale monitor `ws_285b5bf215fd4b329eb1af65`; implements section/shape-aware protected-file guardrails for legitimate dependency/workflow edits without allowing CI or coverage bypasses. |
| P0 AWF Dogfood Stability | Automate preserved-active restart recovery | `ws_53702c4210de4ec59e9ec059` | [#272](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/272) | merged | Merged 2026-05-21. Fresh monitor replaced cancelled stale monitor `ws_77bb4cce4aea4892bb41e0e6`; closes the preserved-active execution reattach/recovery gap after service or worker restarts. |
| P0 / P1 AWF Dogfood Stability | Bootstrap env and PR-monitor recovery stability | `ws_677ab22a7f7b4b7abc02ea57` | [#267](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/267) | merged | Merged 2026-05-19. Carries local bootstrap env, validation-handoff, salvage safe-directory, and provider-circuit monitor recovery fixes. |
| P1 Merge Safety And PR Monitor Correctness | Separate advisory PR feedback from merge-blocking reviews | `ws_e718fac4c82c41d4baa143af` | [#269](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/269) | merged | Merged 2026-05-20. Salvaged clean work from failed `ws_0d8e6ceeb322430daa745ad6`; PR monitor no longer treats advisory `COMMENTED` reviews and top-level bot comments as merge blockers while preserving them for the address loop. |
| P1 API Contract Completion | Expose workspace create effort across REST, CLI, and MCP | _local_ | [#271](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/271) | merged | Merged 2026-05-20. Adds `task.effort` to `POST /v1/workspaces`, persists it as `task_policy.agent_effort`, exposes CLI `--effort`, exposes MCP `effort`, regenerates `openapi.json`, and adds API/CLI/MCP/contract regressions. |
| P1 MCP And Project Onboarding Client Parity | Harden CLI `adopt-pr` API root handling | `ws_d8b9fd1a13c34e68a866be84` | [#266](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/266) | merged | Monitor-only adoption merged 2026-05-19. Replaced failed Spark retry `ws_eba3d6c717f3491bb4d2c367` after AWF fix `200754ae` persisted monitor provider-circuit cooldown as durable provider-recovery state. |
| P1 Local Service Readiness | Make local service host ports configurable | `ws_5ffbf983ef9c45e1b5f74ee6` | [#265](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/265) | merged | Monitor-only adoption merged 2026-05-20. Replaced failed Spark retry `ws_8e051db584564e9d9bd97566` after AWF fix `200754ae` persisted monitor provider-circuit cooldown as durable provider-recovery state. |
| P1 MCP And Project Onboarding Client Parity | Make smoke validation honor repo profile | `ws_c046c2c6364340c298786c3e` | [#263](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/263) | merged | Monitor-only adoption merged 2026-05-19 after the local merge-queue head-of-line bug was fixed and AWF was rebuilt/restarted. Replaced failed Spark monitor `ws_b9fe80a12ae04c9b849c43ae`. |
| P1 Test Coverage And Quality Gates | Add fallback focused repro commands for CI pytest evidence | `ws_a1b0d9e586c644d1ba4b5d60` | [#258](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/258) | merged | Completed and merged 2026-05-16 with GitHub CI green. PR #256 dogfood showed GitHub CI evidence can contain failing pytest node IDs while `suggested_repro_commands` remains empty; this slice adds generic bounded fallback repro commands without hardcoding AWF check names or asking agents to rediscover known failures through broad local coverage. |
| P1 Security, Secrets, And Egress Policy | Add bounded request admission for workspace creation and callback registration | `ws_8b76839898f1400abc16ad08` | [#256](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/256) | merged | Clean retry completed and merged 2026-05-16 with GitHub CI green. This retry replaced cancelled `ws_b7017872938042129fd09d33`, whose stale worker image ignored `validation.strategy.final_gate: none`; the final monitor repair addressed request-admission/idempotency ordering and coverage gaps before merge. |
| P1 Security, Secrets, And Egress Policy | Complete low-risk security cleanup audit | `ws_7bad4fd57a2b4995acc9292a` | [#257](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/257) | merged | Completed and merged 2026-05-15 with GitHub CI green. Scope was intentionally narrow: replace fragile SQL interval interpolation, reduce selected 409/error internal field leakage, and prove doctor known-secret sets are redaction-only. |
| P1 Security, Secrets, And Egress Policy | Add production configuration footgun guardrails | `ws_084580a1fa544b95bcbcab98` | [#255](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/255) | merged | Clean retry completed and merged 2026-05-15 with GitHub CI green. Replaced cancelled `ws_6f426618098f4361be6e4354`, whose stale worker image ignored `validation.strategy.final_gate: none` and entered repeated local full-coverage repair loops. |
| P1 Security, Secrets, And Egress Policy | Callback auth and SSRF delivery hardening | `ws_60589ae904754135b70e6e9f` | [#249](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/249) | merged | Monitor-only adoption completed and merged 2026-05-15 with GitHub CI green. Replaced failed Spark workspace `ws_e56b535618c649cdb5a60999`, which hit Codex Spark capacity during PR comment repair and stale-active terminalization before the local provider-recovery guard. |
| P1 MCP And Project Onboarding Client Parity | Expose `adopt-pr` model and effort selection | `ws_163f54bbf14e4ad18f8bc16a` | [#254](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/254) | merged | Completed and merged 2026-05-15 with GitHub CI green. Adds optional model/effort selection for PR monitor adoption and defaults effort to the highest appropriate setting for the chosen model. |
| P0 Test Coverage And Quality Gates | Make workspace-local parallel final coverage deterministic | `ws_716851d0d48f4ff69bcc41ad` | [#252](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/252) | merged | Completed and merged 2026-05-15. Note: the fix is present in latest `codex/awf-post-merge-fixes`; the local service had to be rebuilt afterward so the worker would honor `validation.strategy.final_gate: none`. |
| P0 Test Coverage And Quality Gates | Make workspace setup dependency installs resilient and cache-aware | `ws_0e15317e2baa44328c40f81e` | [#248](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/248) | merged | Completed and merged 2026-05-15 with GitHub CI green; transient dependency/DNS setup fetch failures are retried/classified by AWF instead of surfacing as opaque service startup failures. |
| P1 Security, Secrets, And Egress Policy | API auth posture and timing-safe token checks | `ws_c63623b7d5194bfa83cc702e` | [#250](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/250) | merged | Monitor-only adoption replaced failed monitor `ws_95ce188d34484e5093b727c5`; completed and merged 2026-05-15 after GitHub CI went green. |
| P1 API Contract Completion | Collapse workspace create to one canonical v1 API and remove stale docs/scripts | _local_ | [#260](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/260) | merged | Merged 2026-05-17. Collapses the rich workspace-create contract into `POST /v1/workspaces`, removes the former duplicate create route/tool, retires legacy operator scripts, updates public docs/backlog guidance, and passes focused API/CLI/MCP/contract/docs validation plus lint/type/OpenAPI drift checks. |
| P1 MCP And Project Onboarding Client Parity | Complete workspace create CLI and MCP policy parity | `ws_4599ede79dce445790f4c6e4` | [#247](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/247) | merged | Monitor-only adoption replaced failed Spark workspace `ws_61e0f7b210fa423faef0b6f3`, handled PR monitoring on Codex `gpt-5.5`, and merged 2026-05-14 with all GitHub CI checks green. |
| P0 Operation And Recovery Truth | Preserve primary failure causality across stale callbacks and recovery paths | `ws_7038898eac3747ecaa53fb2c` | [#242](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/242) | merged | Codex `gpt-5.5`; completed 2026-05-14 and preserves primary validation/provider failure causality across stale callbacks, recovery/remonitor epochs, cleanup/runtime secondary failures, and worker reconnect paths. |
| P0 API / CLI / MCP Contract Parity | Make workspace create/list surfaces parity-safe across REST, CLI, and MCP | `ws_f9c0654695334f2386c2c7eb` | [#246](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/246) | merged | Gemini `gemini-3.1-pro-preview`; clean retry completed 2026-05-14 after failed `ws_02ef6b49f7dc4657a8e63355` and superseded planning-scope failure `ws_b9112aecd2d94fc7b4babf26`; adds CLI/API/MCP create/list parity and active multi-status list semantics. |
| P1 MCP And Project Onboarding Client Parity | Add MCP parity for global events | `ws_32a3971e4aa147c08ed46683` | [#245](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/245) | merged | OpenCode/Ollama `ollama/glm-5.1:cloud`; clean retry completed 2026-05-14 after failed `ws_cd0ccbb17db943ed8415aff1` and `ws_dabd5b60a8464f10b927f1d2`; adds the global events MCP parity surface. |
| P1 Developer Experience And Public Core Surface | First-run troubleshooting guide by symptom | `ws_91940abf598341b789390979` | [#243](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/243) | merged | Codex Spark `gpt-5.3-codex-spark`; completed 2026-05-14 and adds a first-run troubleshooting guide organized by symptom. |
| P0 Control-Plane Restart Recovery Hardening | Expire preserved active executions and keep planning-scope retries phase-scoped | _local_ | commit `89ea11f` | merged | Local Codex implementation; completed 2026-05-14 and fixes two dogfood blockers: preserved active executions now expire and are cleaned up instead of leaving sleeping containers marked `running`, and planning-scope retry prompts no longer globally tell the retry workspace to stop after planning. |
| P0 PR Monitor Stability | Treat concurrent base-fetch remote-tracking ref lock races as transient | _local_ | commit `19c3ba99` | merged | Local Codex implementation; completed 2026-05-14 after `ws_f9c0654695334f2386c2c7eb` failed in monitoring with `GIT_FETCH_BASE_FAILED` despite PR #246 being CI-green. The root cause was a concurrent `git fetch` updating `refs/remotes/origin/codex/awf-post-merge-fixes` between Git's expected-old and lock update; AWF now retries that race through the existing transient base-fetch path instead of marking the workspace failed. |
| P1 MCP And Project Onboarding Client Parity | Close REST auth parity for operation read endpoints | `ws_94e4afca890d47b584208bfc` | [#244](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/244) | merged | Gemini `gemini-3.1-pro-preview`; completed 2026-05-14 and closes REST auth parity for operation read endpoints with focused tests/docs. |
| P0 Test Coverage And Quality Gates | Robust post-agent pre-commit recovery | _local_ | [#239](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/239) | merged | Local Codex implementation; completed 2026-05-13 and generalizes post-agent commit/pre-commit classification, deterministic formatter/normalizer repair, targeted semantic repair, and original failure-causality preservation for timeout/provider failures. |
| P0 Provider Resilience And Automated Fallback Recovery | Provider auth failure classification for PR monitor recovery | _local_ | [#240](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/240) | merged | Local Codex implementation; completed 2026-05-13 and classifies provider-auth failures with `PROVIDER_AUTH_FAILED` instead of treating them as generic agent/CI repair failures. |
| P0 Test Coverage And Quality Gates | Feed GitHub Actions failure evidence into PR-monitor repair turns | _local_ | [#241](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/241) | merged | Local Codex implementation; completed 2026-05-14 and extracts redacted GitHub Actions failure evidence, focused repro commands, failing tests/errors, and check metadata for repair prompts without asking agents to rediscover known CI failures through broad local coverage. |
| P1 MCP And Project Onboarding Client Parity | Bounded MCP artifact content read tool | `ws_01349ca4ecca408baff1d446` | [#238](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/238) | merged | OpenCode/Ollama `ollama/kimi-k2.6:cloud`; completed 2026-05-13 and adds bounded MCP artifact content reads with size/error guardrails and matching docs/reason-catalog coverage. |
| P0 Reliability, Cleanup, And SLOs | Stop and release terminal failed runtime resources without destroying salvage evidence | `ws_de86ae75f42943d1830f1b0c` | [#236](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/236) | merged | Codex `gpt-5.5`; completed 2026-05-12 and stops terminal failed/cancelled/completed runtime stacks while preserving logs, artifacts, worktree/branch salvage metadata, failure diagnostics, and readiness distinction between retained evidence and leaked live resources. |
| P0 Reliability, Cleanup, And SLOs | Harden Postgres/asyncpg connection resilience for long-running local control planes | `ws_b9cdd9b1c3474951876ee21d` | [#227](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/227) | merged | Codex `gpt-5.5`; completed 2026-05-10 and adds SQLAlchemy/asyncpg liveness, bounded invalidation/retry behavior, worker polling continuity, and service-health diagnostics for closed DB connections. |
| P0 Operation And Recovery Truth | Add an AWF-owned conformance-to-validation handoff | `ws_c76512d8b0514eff9a3c8a38` | [#225](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/225) | merged | Codex `gpt-5.5`; completed 2026-05-09 and routes missing/stale AWF validation evidence from conformance into AWF-owned validation/provenance, then reruns conformance while keeping real plan/API gaps routed to agent iteration. |
| P1 Developer Experience And Public Core Surface | Document and demo existing PR monitor adoption | `ws_e332a1d013c54928863320f0` | [#214](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/214) | merged | Codex `gpt-5.5`; completed 2026-05-08 after rebase on PR #216 and review feedback. Adds the canonical PR monitor adoption runbook and REST/CLI/MCP docs/demo coverage aligned with terminal retry behavior. |
| P0 Operation And Recovery Truth | PR adoption terminal idempotency hardening | `ws_e5b86a598da842e0aaf50d1f` | [#216](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/216) | merged | Codex `gpt-5.5`; completed 2026-05-08 and prevents terminal adoption rows from satisfying live PR monitor adoption idempotency. Follow-up [#222](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/222) covers fresh adoption after terminal monitors. |
| P1 API Contract Completion | REST CLI MCP contract parity tests | `ws_3a9bb03983e343e28f462e3e` | [#218](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/218) | merged | Codex `gpt-5.5`; completed 2026-05-08 and extends executable REST/CLI/MCP contract parity across request/response fields, idempotency, auth/error shapes, and intentional partial surfaces. |
| P1 MCP And Project Onboarding Client Parity | CLI command coverage alignment | `ws_5caa27f35e9e4161a312a1b8` | [#206](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/206) | merged | Codex Spark `gpt-5.3-codex-spark`; completed 2026-05-08 and adds CLI coverage for workspace controls plus operations list/show parity. Supersedes the destroyed adoption monitor `ws_657b484a622544b6aee70924`. |
| P0 Control-Plane Restart Recovery Hardening | Adopt or preserve active executions after worker restart | `ws_13dd6ba7165141c285bd771e` | [#219](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/219) | merged | Codex `gpt-5.5`; completed 2026-05-07 and preserves healthy live agent/validation/push executions across worker restart or in-memory task-map loss instead of tearing them down as stale-active. |
| P0 Operation And Recovery Truth | PR monitor merge completion observability | `ws_b078879df0dd417784afb8b9` | [#221](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/221) | merged | Codex `gpt-5.5`; completed 2026-05-07 and adds structured pre-merge settle logging plus workspace-level merge SHA persistence. |
| P0 Operation And Recovery Truth | Allow fresh adoption after terminal PR monitor | `ws_6bdf949938c14ad3b4a1d58e` | [#222](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/222) | merged | Codex `gpt-5.5`; completed 2026-05-07 and keeps cancelled/failed/destroyed adoption rows auditable while allowing a fresh monitor for the still-open PR. |
| P0 Test Coverage And Quality Gates | Coverage fail-under merge gate | _local_ | [#224](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/224) | merged | Dedicated branch `codex/coverage-command-gate`; completed 2026-05-07 and makes coverage-provider fail-under output authoritative even when rounded total coverage appears to meet the threshold. |
| P0 Merge Safety And PR Monitor Correctness | Reviewer recovery and merge queue blocker hardening | _local_ | [#223](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/223) | merged | Dedicated branch `codex/pr-monitor-merge-blockers`; completed 2026-05-07 and restricts merge queue blockers to older overlapping candidates while suppressing superseded review-bot bookkeeping noise. |
| P0 Operation And Recovery Truth | Adopted PR recovery pushes to real head | _local_ | [#220](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/220) | merged | Dedicated branch `codex/sync-feature-pr-recovery-head`; completed 2026-05-07 and makes validate-only recovery for adopted PRs push fixes back to the real remote PR head. |
| P1 Security, Secrets, And Egress Policy | Outbound egress audit evidence | `ws_7e7f6d54bc924c47a5723621` | [#212](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/212) | merged | OpenCode/Ollama `deepseek-v4-pro:cloud`; completed 2026-05-06 and records redacted policy-controlled egress audit summaries in service, workspace, MCP, metrics, and console surfaces. |
| P1 MCP And Project Onboarding Client Parity | MCP safe operator action tools | `ws_524e6b90877a429e9209f70d` | [#211](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/211) | merged | OpenCode/Ollama `kimi-k2.6:cloud`; completed 2026-05-05 and adds bounded MCP tools for retry, remonitor, refresh, validate, rebase, cancel, stop, and destroy. |
| P1 Developer Experience And Public Core Surface | Stable OpenAPI artifact and API examples | `ws_2f77344b87ba4cf987d62cbe` | [#210](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/210) | merged | OpenCode/Ollama `glm-5.1:cloud`; completed 2026-05-05 and publishes the checked-in OpenAPI artifact plus copy-paste API examples and drift tests. |
| P1 API Contract Completion | REST CLI MCP contract alignment tests | `ws_72055a2cbe8148b4a7d1468e` | [#209](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/209) | merged | Claude Code `claude-opus-4-7`; completed 2026-05-05 and adds the first contract alignment pass for request/response/reason/idempotency/auth/error parity. |
| P1 Operator Console Completion | Live workspace activity signals | `ws_ce68a96b836442eb96a1255a` | [#208](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/208) | merged | Gemini `gemini-3.1-pro-preview`; completed 2026-05-05 and surfaces last activity/log times, active subphase, and stale-running warnings. Supersedes failed workspace `ws_681eec29b3e44a0daa4a0264`. |
| P1 MCP And Project Onboarding Client Parity | Parity matrix status drift guard | `ws_0d178aec82324b7bb8b8bc3a` | [#207](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/207) | merged | Claude Code `claude-opus-4-7`; completed 2026-05-05 and guards the MCP parity matrix against drift from real REST/CLI/MCP surfaces. |
| P1 Developer Experience And Public Core Surface | Public docs search and readability checks | `ws_0bc1d8f718bc480998d0a08d` | [#217](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/217) | merged | Codex `gpt-5.5`; completed 2026-05-06 and adds public docs discoverability/readability checks for guide links, CLI command references, and copy-paste snippet validity. |
| P1 MCP And Project Onboarding Client Parity | Docs/status consistency test for the parity matrix | `ws_aeec0296eee64c869d328ae2` | [#215](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/215) | merged | Codex `gpt-5.5`; completed 2026-05-06 and adds executable consistency checks tying parity-matrix implemented/partial/backlog statuses to real REST, CLI, MCP, and contract-test evidence. |
| P0 Reliability, Cleanup, And SLOs | Readiness retained worktree and service GC root alignment | `ws_e9562a751e4c4cd599a66856` | [#213](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/213) | merged | Dedicated branch `codex/readyz-retained-worktree-gc-root`; completed 2026-05-06 and fixes retained terminal worktrees incorrectly failing `/readyz` plus local service GC resolving the repo `.awf` path instead of the service work root. |
| P1 Validation Runtime Performance | Safe parallel final coverage support | _local_ | commit `9763bd9` | merged | Reconciled 2026-05-05: `pytest-xdist` is in the dev/test runtime, profiles accept `validation.coverage.parallel_workers`, `.awf/workspace.yml` opts AWF self-dogfood into `parallel_workers: 3`, coverage commands inject bounded `pytest -n <workers> --dist=loadscope`, worker policy is capped by CPU/profile limits, and validation identity includes the parallel-worker policy. |
| P1 Local Packaging And Upgrade Path | Auto-prune completed and merged workspace worktrees | `ws_e90d1b2cf47a45cf920f01a0` | [#203](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/203) | merged | OpenCode/Ollama `glm-5.1:cloud`; merged 2026-05-04 and adds policy-safe completed/merged workspace worktree pruning with retention safeguards. |
| P1 Developer Experience And Public Core Surface | Redacted first-time support bundle | `ws_02a6a86cac9a492c8562d164` | [#202](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/202) | merged | OpenCode/Ollama `kimi-k2.6:cloud`; merged 2026-05-04 and adds telemetry-free redacted doctor/support-bundle evidence for first-time evaluator issue reports. |
| P1 Operator Console Completion | Reliable cost estimate surfacing | `ws_f0c067d4523f480ea6d7c8ec` | [#201](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/201) | merged | OpenCode/Ollama `deepseek-v4-pro:cloud`; merged 2026-05-04 and limits console cost estimates to trusted pricing and usage metadata. |
| P1 Operator Console Completion | Token usage surfacing | `ws_61dcb9386b83477a9a6efbce` | [#200](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/200) | merged | Gemini `gemini-3.1-pro-preview`; retry of `ws_68935277abb74e619a06b232`, merged 2026-05-04 and surfaces nullable provider token usage without inventing values. |
| P1 Operator Console Completion | Stable wide-screen embedded inspector | `ws_f33b10c9e5f8445aaaaced7d` | [#199](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/199) | merged | Gemini `gemini-3.1-pro-preview`; merged 2026-05-04 and keeps global dashboard panes stable while the embedded workspace inspector opens and closes. |
| P1 MCP And Project Onboarding Client Parity | PR monitor adoption for existing GitHub PRs | `ws_23dd9badf9fe4290a51113e7` | [#198](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/198) | merged | Codex `gpt-5.5` with AWF `xhigh`; merged 2026-05-05 and adds first-class REST/CLI/MCP service-managed monitor adoption for existing PRs. |
| P1 Security, Secrets, And Egress Policy | Supply-chain guardrails for agent package installs | `ws_a1357eb1d1db498a9ed499ed` | [#197](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/197) | merged | Codex `gpt-5.5` with AWF `xhigh`; merged 2026-05-05 after parser hardening and adds profile-selectable warn/block supply-chain policy. |
| P1 Developer Experience And Public Core Surface | Searchable reason-code catalog | `ws_46678fbed83645709bfa6771` | [#196](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/196) | merged | Gemini `gemini-3.1-pro-preview`; MacBook validation-runtime dogfood batch completed successfully and merged after AWF PR monitoring. |
| P1 Developer Experience And Public Core Surface | README split into focused public Core docs | `ws_f598e969bed54d17be999e62` | [#195](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/195) | merged | Gemini `gemini-3.1-pro-preview`; MacBook validation-runtime dogfood batch completed successfully and merged after AWF PR monitoring. |
| P1 Developer Experience And Public Core Surface | First-time CLI help text | `ws_a062174bfc9948e480d05c2b` | [#194](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/194) | merged | Gemini `gemini-3.1-pro-preview`; MacBook validation-runtime dogfood batch completed successfully and merged after AWF PR monitoring. |
| P1 Developer Experience And Public Core Surface | SDK stance for Core release | `ws_70cbcbca1ce94e908015b9b6` | [#193](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/193) | merged | Gemini `gemini-3.1-pro-preview`; MacBook validation-runtime dogfood batch completed successfully and merged after AWF PR monitoring. |
| P1 Developer Experience And Public Core Surface | Start Here quickstart | `ws_961c1b63e36d461ea3bb14dd` | [#192](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/192) | merged | Gemini `gemini-3.1-pro-preview`; MacBook validation-runtime dogfood batch completed successfully and merged after AWF PR monitoring. |
| P1 MCP And Project Onboarding Client Parity | MCP read tools for operator surfaces | `ws_b8f4de29ba874a3092f1b7f6` | [#191](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/191) | merged | Codex `gpt-5.5`; first Linux-machine completed AWF workspace. Recovered after local false `PLAN_ONLY_OUTPUT` fix, then AWF validated 4,089 tests with 99.03% coverage and merged PR #191 on 2026-05-03. |
| P1 MCP And Project Onboarding Client Parity | Local control-plane UID/GID strategy | `ws_add274bc03eb49c28a00dd3d` | [#186](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/186) | merged | Manually salvaged 2026-05-03: dropped the out-of-scope workflow edit, addressed the image-missing review inside the integration test, force-pushed cleaned head `655ad145`, requested AWF remonitor, and PR merged 2026-05-03. |
| P1 MCP And Project Onboarding Client Parity | Launch-time provider readiness preflight | `ws_6dcca29a9a4e47cd89e0c8c7` | [#189](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/189) | merged | Codex `gpt-5.5`; recovered on 2026-05-03 after local AWF monitor fix `ebfad0a` stopped active-recovery remonitor loops and operator remonitor cancelled stale monitor recovery op `op_4daeba7e12904635a68bd8ea`. AWF synced base, revalidated, waited non-check reviewer settle, and merged PR #189 at `3f938345`. |
| P1 MCP And Project Onboarding Client Parity | First-run smoke workspace command | `ws_cd491b1fdb514174974ed549` | [#188](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/188) | merged | OpenCode/Ollama `deepseek-v4-pro:cloud`; validate-only recovery completed, AWF monitored checks/comments/merge gates, and PR merged 2026-05-03. |
| P1 MCP And Project Onboarding Client Parity | Copy-paste agent onboarding prompts | `ws_55479d5e2367490184a947ea` | [#185](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/185) | merged | OpenCode/Ollama `kimi-k2.6:cloud`; merged 2026-05-03 after candidate provenance repair and validate-only recovery. |
| P1 MCP And Project Onboarding Client Parity | Primary local Core install path | `ws_9e695c9961bb45f9a9b1ff8b` | [#187](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/187) | merged | Gemini `gemini-3.1-pro-preview`; completed 2026-05-03 and marks the primary package-manager install path P1 done. |
| P1 Security, Secrets, And Egress Policy | Restricted egress allowlist templates | `ws_acf536de3fe3434699bee650` | [#183](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/183) | merged | OpenCode `ollama/kimi-k2.6:cloud`; landed as `0f65407` and reconciled from stale active ledger on 2026-05-03. |
| P1 MCP And Project Onboarding Client Parity | API / CLI / MCP parity implementation driver | `ws_f4f5d0934e5f45c1ba0d7998` | [#182](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/182) | merged | OpenCode `ollama/glm-5.1:cloud`; landed as `2d23a5a` and reconciled from stale active ledger on 2026-05-03. |
| P1 Operator Console Completion | Agent and exact model workspace filters | `ws_0e5f80c0f2be464db625c766` | [#181](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/181) | merged | Gemini `gemini-3.1-pro-preview`; landed as `8a89d54` and reconciled from stale active ledger on 2026-05-03. |
| P1 MCP And Project Onboarding Client Parity | One-command `awf init` local bootstrap | `ws_0da5b57348cb49d198db9ee2` | [#180](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/180) | merged | Claude Code `claude-opus-4-7`; landed as `8f40b39` and reconciled from stale active ledger on 2026-05-03. |
| P1 Security, Secrets, And Egress Policy | Prompt-injection boundary controls for external evidence | `ws_738700a49275436b9b96ec7e` | [#179](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/179) | merged | Codex `gpt-5.5`; landed as `65d71f8` and reconciled from stale active ledger on 2026-05-03. |
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
| P1 API Contract Completion | Guard legacy endpoint compatibility | `ws_a41728907dc740d6a1ae7092` | [#157](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/157) | merged | Historical compatibility guard from before the single-API simplification. |
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
| P1 Local Packaging And Upgrade Path | Local backup, upgrade, and recovery runbook | `ws_f3145b63327c482aaaa37c10` | [#132](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/132) | merged | Documents local image versioning, Postgres backup/restore, rollback, and disaster recovery for the service-backed workflow. |
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

| TODO area | Slice | Workspace | PR | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Merge Safety And PR Monitor Correctness | Separate advisory PR feedback from merge-blocking reviews | `ws_0d8e6ceeb322430daa745ad6` | [#269](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/269) | superseded | Original implementation workspace completed focused tests, ruff, mypy, and committed `9c9ea191`, but failed before PR creation after the local service restart lost the in-process execution task. AWF emitted `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART`, preserved the live runtime, but the replacement worker never reattached; stale-active detection later emitted `STALE_ACTIVE_EXECUTION`, stopped the runtime, and marked the workspace failed. The clean committed branch was pushed manually and fresh monitor `ws_e718fac4c82c41d4baa143af` now owns PR #269. Root cause: AWF restart recovery can preserve a live runtime without an execution reattach/resume path, then terminalize useful completed work. |
| P1 MCP And Project Onboarding Client Parity | Make smoke validation honor repo profile | `ws_b9fe80a12ae04c9b849c43ae` | [#263](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/263) | superseded | Completed implementation, focused validation, PR creation, and several PR comment-repair pushes, then failed in PR monitoring after `openai/gpt-5.3-codex-spark` entered provider/model circuit cooldown. Root cause matches the later Spark monitor failures: the old AWF image emitted only `workspace.provider_recovery_cooldown` without persisted `task_policy.provider_recovery_state`, so stale-active cleanup marked the paused monitor failed. Fresh monitor `ws_c046c2c6364340c298786c3e` now owns PR #263. |
| P1 MCP And Project Onboarding Client Parity | Harden CLI `adopt-pr` API root handling | `ws_eba3d6c717f3491bb4d2c367` | [#266](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/266) | superseded | Retry completed implementation, focused validation, push, and PR creation, then failed in PR monitoring after `openai/gpt-5.3-codex-spark` entered provider/model circuit cooldown. Root cause was an AWF monitor recovery bug: the circuit-open fast path emitted only `workspace.provider_recovery_cooldown` and did not persist `task_policy.provider_recovery_state`, so stale-active cleanup treated the paused monitor as abandoned. Local fix `200754ae` records durable retry state; fresh monitor `ws_d8b9fd1a13c34e68a866be84` now owns PR #266 with Codex `gpt-5.5`/`xhigh`. |
| P1 Local Service Readiness | Make local service host ports configurable | `ws_8e051db584564e9d9bd97566` | [#265](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/265) | superseded | Retry completed implementation, focused validation, push, and PR creation, then failed in PR monitoring after `openai/gpt-5.3-codex-spark` entered provider/model circuit cooldown. Root cause was an AWF monitor recovery bug: the circuit-open fast path emitted only `workspace.provider_recovery_cooldown` and did not persist `task_policy.provider_recovery_state`, so stale-active cleanup treated the paused monitor as abandoned. Local fix `200754ae` records durable retry state; fresh monitor `ws_5ffbf983ef9c45e1b5f74ee6` now owns PR #265 with Codex `gpt-5.5`/`xhigh`. |
| P1 MCP And Project Onboarding Client Parity | Harden CLI `adopt-pr` API root handling | `ws_304d823a6c084c82990de920` | none | superseded | Failed 2026-05-18 in conformance despite completing implementation and focused validation. Root cause was an AWF conformance-classifier bug: `CONFORMANCE_REQUIRES_AWF_VALIDATION` gaps that explicitly named validation commands were rejected when the command text included `tests/...` or `src/...` paths, so AWF looped the agent instead of handing off to AWF-owned validation. First retry was also blocked by salvage Git `safe.directory` handling, then reached PR #266 as `ws_eba3d6c717f3491bb4d2c367`; PR monitoring is now owned by `ws_d8b9fd1a13c34e68a866be84`. |
| P1 Local Service Readiness | Make local service host ports configurable | `ws_dba771cf034b4be8b936977e` | none | superseded | Failed 2026-05-18 in conformance despite completing implementation and focused validation. Root cause was an AWF conformance-classifier bug: `CONFORMANCE_REQUIRES_AWF_VALIDATION` gaps that explicitly named validation commands were rejected when the command text included `tests/...` or `src/...` paths, so AWF looped the agent instead of handing off to AWF-owned validation. First retry was also blocked by salvage Git `safe.directory` handling, then reached PR #265 as `ws_8e051db584564e9d9bd97566`; PR monitoring is now owned by `ws_5ffbf983ef9c45e1b5f74ee6`. |
| P1 Security, Secrets, And Egress Policy | Add production configuration footgun guardrails | `ws_6f426618098f4361be6e4354` | none | superseded | Cancelled 2026-05-15 because the local worker image was stale and ignored the already-merged `final_gate: none` executor policy, causing repeated local full-coverage repair loops before PR creation. Branch/worktree/logs are preserved as evidence, but the branch was polluted by broad coverage-threshold repair commits; clean retry `ws_084580a1fa544b95bcbcab98` now owns the slice. |
| P1 Security, Secrets, And Egress Policy | Add bounded request admission for workspace creation and callback registration | `ws_b7017872938042129fd09d33` | none | superseded | Cancelled 2026-05-15 because the local worker image was stale and ignored the already-merged `final_gate: none` executor policy, causing repeated local full-coverage repair loops before PR creation. Branch/worktree/logs are preserved as evidence, but the branch was polluted by broad coverage-threshold repair commits; clean retry `ws_8b76839898f1400abc16ad08` now owns the slice. |
| P0 Test Coverage And Quality Gates | Make workspace-local parallel final coverage deterministic | `ws_82b51b498cd044d2b4646d67` | none | superseded | Attempt had one local commit plus dirty follow-up files, but no PR. It failed 2026-05-14 because a local service restart interrupted the running agent and stale-active cleanup stopped the runtime. Evidence is retained in the failed worktree; clean retry `ws_716851d0d48f4ff69bcc41ad` now owns the slice from current base. |
| P0 Test Coverage And Quality Gates | Make workspace-local parallel final coverage deterministic | `ws_6c3a1f289fe040dfb32cc8d0` | none | failed | Attempt failed 2026-05-14 before agent execution with `SERVICE_STARTUP_FAILURE`: profile setup command `uv sync --extra dev` failed to download `docker==7.1.0` because PyPI DNS lookup returned `No address associated with hostname`. Treat as AWF setup/dependency resilience work, not provider or agent failure. |
| P1 MCP And Project Onboarding Client Parity | Complete workspace create CLI and MCP policy parity | `ws_61e0f7b210fa423faef0b6f3` | [#247](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/247) | superseded | Original Spark monitor failed after Codex `gpt-5.3-codex-spark` capacity/circuit exhaustion and stale-active terminalization. Existing PR branch was preserved and re-adopted by `ws_4599ede79dce445790f4c6e4` using Codex default `gpt-5.5`/`xhigh`. |
| P1 Security, Secrets, And Egress Policy | API auth posture and timing-safe token checks | `ws_7dd27492f4184baf8eb67b81` | [#250](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/250) | superseded | Original Spark monitor failed after Codex `gpt-5.3-codex-spark` capacity/circuit exhaustion and stale-active terminalization. Existing PR branch was preserved and re-adopted by `ws_95ce188d34484e5093b727c5` using Codex default `gpt-5.5`/`xhigh`. |
| P1 Security, Secrets, And Egress Policy | Callback auth and SSRF delivery hardening | `ws_e56b535618c649cdb5a60999` | [#249](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/249) | superseded | Original Spark monitor failed during PR comment repair with `AGENT_PROVIDER_CAPACITY_EXHAUSTED`, entered provider recovery, then was incorrectly terminalized by stale-active cleanup. Existing PR branch was preserved and re-adopted by `ws_60589ae904754135b70e6e9f` using Codex default `gpt-5.5`/`xhigh`. |
| P0 API / CLI / MCP Contract Parity | Make workspace create/list surfaces parity-safe across REST, CLI, and MCP | `ws_b9112aecd2d94fc7b4babf26` | none | superseded | Failed 2026-05-14 because Gemini edited implementation files during the planning phase. AWF correctly blocked the premature implementation and created clean retry `ws_f9c0654695334f2386c2c7eb`; do not salvage the failed branch unless explicitly requested. |

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
- [x] **P1: Separate advisory PR feedback from merge-blocking reviews.**
  Regression source: monitoring PR #470 in `dimileeh/aira-agent` showed AWF
  parked at `NotifyHuman` with a high `unresolved_reviews` count dominated by
  `COMMENTED` bot reviews and top-level advisory comments, despite no
  `CHANGES_REQUESTED` review. Acceptance: preserve the full advisory comment
  list for the address loop; add a merge-gate-only blocking review view based
  on effective `CHANGES_REQUESTED`; keep unresolved inline thread gating
  unchanged; log `blocking_reviews` next to `unresolved_reviews`; and allow
  auto-merge to proceed past GitHub `mergeStateStatus=BLOCKED` when GitHub is
  otherwise mergeable, checks are green, inline threads are resolved, and there
  are zero blocking reviews. Completed by PR #269 with fresh PR-monitor
  workspace `ws_e718fac4c82c41d4baa143af`. Original implementation workspace
  `ws_0d8e6ceeb322430daa745ad6` failed after service restart active-execution
  loss, but its clean committed work was preserved and pushed.

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
- [x] Release deterministic PR adoption task/idempotency slots when an
  adoption workspace is destroyed or otherwise terminal. Regression source: a
  destroyed PR monitor adoption workspace left the deterministic repo/PR
  adoption task/idempotency slot behind, so the first clean re-adoption attempt
  hit HTTP 500 instead of creating a fresh monitor workspace. Acceptance:
  destroy/cancel/fail/supersede paths must mark the prior adoption lineage
  terminal, release or safely supersede deterministic repo/PR adoption slots,
  and allow a subsequent adoption request for the same open PR to create or
  attach to exactly one live monitor. Active live adoption workspaces must still
  be idempotent, active monitor policy mismatches must still return
  `PR_ADOPTION_POLICY_CONFLICT`, and concurrent re-adoption races must be
  conflict-safe. Add regression coverage for destroyed, cancelled, failed, and
  superseded prior adoption rows; stale unique task/idempotency records after
  destroy; active idempotent reattach; policy conflict; and concurrent
  REST/CLI/MCP-visible adoption requests. Evidence: PR
  [#216](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/216)
  hardened terminal adoption idempotency and PR
  [#222](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/222)
  proved clean fresh adoption after terminal PR monitors; both merged on
  2026-05-07/08 with focused PostgreSQL-backed adoption coverage and full
  coverage gates above 99%.
- [x] Add recovery for stranded workspaces whose containers exited but DB state is active.
- [x] Add recovery for active PR workspaces after AWF service restart.
- [x] Add console controls for safe remonitor/refresh/revalidate once API semantics are stable.
- [x] Classify unsatisfied plan conformance failures with structured gaps, retry carry-forward, and salvage hints.
- [x] Preserve primary failure causality across stale callbacks and recovery
  paths. Regression source: `ws_4f44c108a58f46d092f4e411` hit a real final
  coverage pytest failure (`13` errors with 99.02% coverage) and then AWF
  overwrote the actionable validation failure with generic
  `STALE_ACTIVE_EXECUTION`. Acceptance: once a workspace has durable validation
  failure evidence, later stale-active, cleanup, callback, or lease-expiry paths
  must preserve the original failure reason, failing command, failing node IDs,
  coverage percent/threshold, and recovery guidance; secondary infrastructure
  faults should be appended as events/diagnostics rather than replacing the
  root cause. Add tests for validation-failed -> stale scan,
  validation-failed -> cleanup failure, and validation-failed -> worker
  reconnect scenarios. Evidence:
  `ws_7038898eac3747ecaa53fb2c` / PR
  [#242](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/242)
  merged 2026-05-14 and preserves primary failure causality across stale
  callbacks, recovery/remonitor epochs, cleanup/runtime secondary failures, and
  worker reconnect paths.
- [x] Add an AWF-owned conformance-to-validation handoff. Regression source:
  `ws_681eec29b3e44a0daa4a0264` was failed as
  `PLAN_CONFORMANCE_UNSATISFIED` even though conformance reported that the
  implementation appeared complete and only AWF-owned validation evidence was
  missing. Acceptance: if conformance gaps are limited to missing/stale AWF
  validation evidence, AWF must transition to validation, run the required
  profile gates, persist validation provenance/log streams, then rerun
  conformance against that evidence instead of asking the agent to run
  validation during the conformance phase. Deterministic plan/API gaps should
  still go back to the agent. Add reason codes and tests for
  `CONFORMANCE_REQUIRES_AWF_VALIDATION`, validation success -> conformance
  success, validation failure -> validation recovery, and real plan gap ->
  agent iteration. Evidence: `ws_c76512d8b0514eff9a3c8a38` / PR
  [#225](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/225)
  completed 2026-05-09 with implementation and regression coverage for the
  handoff path.

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
- [x] Actually invoke planning-scope recovery from the executor/control loop
  instead of merely exposing retry context. Regression source:
  `ws_6c1132d9e6914d8fb0aeca22` failed 2026-05-14 with
  `AGENT_PLAN_PHASE_SCOPE_VIOLATION`; AWF recorded
  `recovery_strategy=discard_and_replan` and preserved salvage evidence, but it
  did not automatically discard/retry the planning phase or enqueue the
  existing retry workflow. Acceptance: when the planning phase modifies only
  out-of-scope implementation files, AWF must either automatically create a
  clean retry workspace/attempt using the configured planning-scope recovery
  policy, or explicitly mark the policy as operator-only in the task/workspace
  response and backlog docs. Add regression coverage proving a live
  `AGENT_PLAN_PHASE_SCOPE_VIOLATION` does not silently become a terminal
  dogfood dead-end when policy says `discard_and_replan`.
  Fixed locally 2026-05-14: executor now calls the retry workflow after
  persisting the planning-scope failure, records auto-retry requested/skipped
  or failed events, and limits automatic retries to non-retry source
  workspaces. Regression coverage:
  `tests/unit/control/test_executor.py::TestHappyPath::test_planning_profile_fails_when_plan_phase_changes_code`.

## P0: Reliability, Cleanup, And SLOs

- [x] Define and expose rolling creation success, cleanup success, stuck-state, and recovery success metrics.
- [x] Add stuck-state watchdog metrics and actionable reason codes.
- [x] Detect orphan containers, networks, volumes, and worktrees.
- [x] Automatically clean completed PR workspaces after merge and safe retention.
- [x] Preserve logs/artifacts during cleanup according to retention policy.
- [x] Make cleanup idempotent and safe after partial Docker failures.
- [x] Add SLO-style API and console indicators for local AWF health.
- [x] Keep local disk pressure and admission blocking actionable in service status.
- [x] Harden Postgres/asyncpg connection resilience for long-running local
  control planes. Regression source: during the `ws_4f44c108a58f46d092f4e411`
  / `ws_681eec29b3e44a0daa4a0264` run, the worker and API repeatedly hit
  `InterfaceError: connection is closed`, while the API/worker containers had
  not restarted. Acceptance: configure the async SQLAlchemy engine with
  connection liveness checks such as `pool_pre_ping`, sensible recycle/timeout
  behavior, and bounded retry/invalidation around transient closed-connection
  failures in worker and API read paths. The worker must continue polling
  without losing active execution ownership, and closed DB connections must be
  reported as service-health diagnostics without turning unrelated workspaces
  terminal. Add regression tests around worker `run_once`, scheduler reads,
  stale-active scans, and API list/detail calls after a simulated closed
  connection. Evidence: `ws_b9cdd9b1c3474951876ee21d` / PR
  [#227](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/227)
  completed 2026-05-10 with `src/awf/db/resilience.py`, worker/API resilience
  handling, health diagnostics, and regression tests for closed-connection
  paths.
- [x] Stop and release terminal failed runtime resources without destroying
  salvage evidence. Regression source: `ws_681eec29b3e44a0daa4a0264` reached
  terminal `failed` but its agent container and network remained running,
  leaving `/readyz` blocked by orphan terminal resources. Acceptance: when a
  workspace reaches terminal `failed` / `cancelled` / `completed` states, AWF
  must release reservations and stop containers/networks according to retention
  policy while preserving logs, artifacts, worktree/branch salvage metadata, and
  failure diagnostics. `/readyz`, orphan-resource status, and cleanup dry-runs
  should explain retained evidence separately from leaked live resources. Add
  tests for conformance failure, validation failure, stale-active failure,
  cleanup failure, and preserved-worktree salvage paths. Evidence:
  `ws_de86ae75f42943d1830f1b0c` / PR
  [#236](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/236)
  completed 2026-05-12 with terminal runtime teardown, reservation release,
  retained-evidence readiness behavior, and salvage-preserving regression
  coverage.
- [x] Keep readiness and service GC aligned with retained terminal worktree
  policy. Evidence: `ws_e9562a751e4c4cd599a66856` / PR
  [#213](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/213)
  completed 2026-05-06 and fixed retained terminal worktrees incorrectly
  failing `/readyz` plus local service GC resolving the repo `.awf` path instead
  of the service work root.

## P0: Test Coverage And Quality Gates

- [x] Keep branch coverage enabled.
- [x] Keep AWF self-development coverage at 99%+.
- [x] Add coverage reports that explain remaining gaps instead of only failing a threshold.
- [x] Add focused tests for PR monitor recovery, stale detection, validation tier gating, and service restart recovery.
- [x] Add integration tests for two parallel PRs where one merge stales the other.
- [x] Add integration tests for Alembic multi-head detection and automatic merge revision generation.
- [x] Add integration tests for Dockerized project profiles with sidecar services.
- [x] Forbid empty tests, fake assertions, and broad monkeypatching that skips behavior under test.
- [x] **P1: Make protected quality-gate guardrail diff-aware.** Regression
  source: monitoring PR #470 in `dimileeh/aira-agent` showed AWF blocked
  legitimate edits to `pyproject.toml` and `.github/workflows/*.yml` because
  the guardrail treats every protected-file edit as a coverage/CI bypass.
  Acceptance: classify protected-file diffs by section/shape; allow additive
  dependency and metadata changes in `pyproject.toml`; block dependency
  deletions and pytest/coverage/ruff/mypy/build policy edits; allow workflow
  comment-step `continue-on-error` and pinned action bumps; block gate-step
  bypasses, test-command narrowing, and job/step removals; include file,
  section/path, approximate line, and reason in block messages; document the
  policy in `docs/PROTECTED_FILES.md`; and pass the seven unit cases plus
  executor/PR-monitor regressions. Completed by PR
  [#268](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/268),
  merged 2026-05-20 with fresh monitor workspace
  `ws_0367e5e1266d4acdbd13441a`; cancelled stale monitor
  `ws_285b5bf215fd4b329eb1af65` is superseded.
- [x] Repair deterministic post-agent pre-commit hook rewrites before failing
  otherwise-valid workspaces. Regression source: the 2026-05-12 first wave
  failed `ws_06ee567d44eb479bb0f68478`,
  `ws_7614d1ea986841bb9612d59f`, and
  `ws_89f9bcd01ae747d6b4a251b5` after agent/conformance work reached the
  post-agent commit step. All three had provider readiness OK and terminal
  runtime cleanup OK, but AWF emitted `POST_AGENT_COMMIT_PRECOMMIT_FAILED` and
  `infrastructure_failure` because pre-commit hooks modified files or reported
  formatting drift: `end-of-file-fixer` and `trailing-whitespace` touched
  AWF-generated `docs/awf-plans/*.md` / `.conformance.json` artifacts, and
  `awf-ruff-format-check` reported Python files needing formatting. Acceptance:
  pre-normalize AWF-generated plan/conformance artifacts before commit; classify
  deterministic modifying hooks separately from semantic hook failures; accept
  pre-commit's whitespace/EOF modifications once, re-stage only the original
  staged set plus AWF-owned plan/conformance artifacts touched by hooks, run
  scoped `uv run --python 3.12 --extra dev ruff format -- <paths>` when the
  ruff-format hook reports paths, retry the commit once, record a structured
  repair event/outcome, and fail only if semantic hooks such as ruff check,
  mypy, tests, large-file, private-key, or merge-conflict hooks still fail.
  Add TDD coverage for the exact hook combinations from the three failed
  workspaces and keep terminal runtime cleanup/readiness behavior intact. Do
  not reschedule those failed slices from scratch until this P0 is fixed.
  Evidence: PR
  [#239](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/239)
  merged 2026-05-13 and adds generalized post-agent pre-commit classification,
  one-shot deterministic hook repair/retry, targeted semantic repair, and
  regression coverage for the observed hook combinations.
- [x] Treat auto-fixable semantic hook diagnostics as deterministic repair when
  the tool proves the fix is safe and bounded to the staged change set.
  Regression source: `ws_2964d670befc43b8a00f5ad6` failed 2026-05-14 after
  `awf-ruff-check` reported fixable `I001 [*]` / `UP035 [*]` diagnostics on
  `src/awf/mcp/server.py`. AWF correctly classified the hook as semantic and
  sent one targeted repair turn, but the agent hand-edited the imports
  incorrectly and the final commit still failed. Acceptance: post-agent repair
  must parse known hook output for explicit auto-fixable markers, run the
  corresponding project-local fixer only against staged/owned paths
  (for example bounded `uv run --python 3.12 --extra dev ruff check --fix --`
  for Ruff diagnostics marked `[*]`), restage and retry once, while keeping
  non-fixable lint/type/test/security hook failures on the targeted agent or
  terminal path. Add tests proving this is a generic hook-fixability policy,
  not a hardcoded "ruff failed" bypass.
  Fixed locally 2026-05-14: executor now parses Ruff `[*]` diagnostics,
  intersects repair paths with the staged Python diff, runs bounded
  `ruff check --fix`, restages, retries once, and records
  `repair_strategy=deterministic_autofix`. Regression coverage:
  `tests/unit/control/test_executor_post_agent_commit_classifier.py::test_ruff_check_fixable_diagnostics_expose_bounded_autofix_paths`
  and
  `tests/unit/control/test_executor_post_agent_commit.py::test_post_agent_commit_autofixable_ruff_check_runs_bounded_fix_before_agent`.
- [x] Make workspace setup dependency installs resilient and cache-aware.
  Regression source: `ws_6c3a1f289fe040dfb32cc8d0` failed 2026-05-14 before
  any agent execution because profile setup ran `uv sync --extra dev` and could
  not DNS-resolve/download `docker==7.1.0` from PyPI
  (`No address associated with hostname`). Acceptance: dependency/network setup
  failures must be classified separately from agent/provider failures, include
  the failing package/index/reason in redacted diagnostics, use bounded retry
  with backoff for transient DNS/connect/read failures, prefer the existing uv
  cache or an AWF-managed wheel/cache strategy where safe, and block or retry
  setup before spending provider time. Recovery must preserve task lineage so a
  setup-only transient can be retried without duplicating the slice. Add tests
  with fake setup runners for transient PyPI/DNS failure, cache-hit recovery,
  exhausted retry classification, and no accidental dependency skipping.
  Operator retry after DNS recovered produced active retry
  `ws_82b51b498cd044d2b4646d67`, proving the original failure was transient and
  that AWF should automate this recovery path instead of requiring manual
  diagnosis.
- [x] Make AWF self-dogfood parallel final coverage deterministic inside the
  workspace runtime. Regression source: `ws_4f44c108a58f46d092f4e411` ran
  `pytest -n 3 --dist=loadscope --cov=awf --cov-report=term-missing`, reached
  99.02% coverage, but still produced `13` pytest errors/timeouts. Acceptance:
  xdist-unsafe tests must be fixed, isolated with explicit serial grouping, or
  given deterministic fixture cleanup; no hidden skips. When coverage meets the
  threshold but pytest fails, AWF must classify the result as a validation test
  failure with failing node IDs and route it through validation recovery, not
  collapse into infra/stale execution. Add workspace-runtime regression coverage
  for `parallel_workers: 3` and local stress coverage for the self-profile.
- [x] Feed GitHub Actions failure evidence into PR-monitor repair turns.
  Regression source: PR #238 / `ws_01349ca4ecca408baff1d446` repeatedly failed
  GitHub `python-full-coverage` on one catalog test, but the monitor repair loop
  asked the agent to rediscover the failure by running broad/full coverage
  locally. The actual actionable evidence was already in GitHub Actions:
  `tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage` reported
  missing `ARTIFACT_BLOCKED` and `ARTIFACT_OVERSIZED` entries in
  `docs/REASON_CATALOG.md`. Acceptance: for any failed GitHub Actions check,
  not just AWF's full coverage job, AWF must fetch the failing job/log evidence,
  extract concise failing commands, test node IDs, assertion snippets, error
  summaries, and check/job names, redact secrets, and pass that evidence into
  the PR-monitor repair prompt as quoted external evidence. Agents should run
  focused repro commands suggested by the failure evidence before broad local
  suites; AWF must not ask agents to run full coverage locally merely to
  discover a known CI failure. Add generic tests for single-test failures,
  multiple-test failures, non-test command failures, unavailable log artifacts,
  secret redaction, and provider-neutral check names. Evidence: PR
  [#241](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/241)
  merged 2026-05-14 and feeds structured, redacted GitHub Actions failure
  evidence plus focused repro commands into PR-monitor repair prompts.
- [x] Add fallback focused repro commands when CI evidence has pytest node IDs
  but no extractable pytest command prefix. Regression source: PR #256 /
  `ws_8b76839898f1400abc16ad08` extracted two failing pytest node IDs from
  GitHub Actions full-coverage evidence, but the PR-monitor payload still had
  `suggested_repro_commands=[]`. Acceptance: when failed CI logs yield pytest
  node IDs but command extraction cannot identify a pytest invocation, AWF
  synthesizes a bounded, shell-quoted fallback command such as
  `uv run --python 3.12 --extra dev pytest <nodes> -q`; the fallback remains
  provider/check-name neutral, does not request broad local full coverage to
  rediscover known failures, and is covered by focused extractor/prompt tests.
  Evidence: PR [#258](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/258)
  merged 2026-05-16 via monitor workspace `ws_a1b0d9e586c644d1ba4b5d60`.

## P1: Validation Runtime Performance

- [x] Add safe parallel final coverage support for AWF self-dogfood and large
  projects. Acceptance: `pytest-xdist` is available in the dev/test runtime;
  profiles can declare `validation.coverage.parallel_workers`; AWF injects a
  bounded `pytest -n <workers>` only when the profile opts in, never `-n auto`;
  worker count is capped by workspace CPU reservation or an explicit policy
  maximum; known xdist-unsafe tests are isolated with serial markers, grouping,
  or fixture cleanup rather than hidden skips; coverage evidence identity
  includes the parallel-worker policy; and the AWF self-profile proves the full
  final coverage gate passes at 99%+ with `parallel_workers: 3` before enabling
  it by default. Background experiment on 2026-05-04: full
  `pytest -n 3 --dist=loadscope --cov=awf --cov-report=term-missing` finished
  in about 4m33s but failed with 30 xdist/shared-state failures, so this slice
  must first make the suite parallel-safe.

## P1: Security, Secrets, And Egress Policy

2026-05-14 review reconciliation: the current-branch review against
`origin/development` and `docs/awf_prd_v2.2.md` found six security/industrial
readiness gaps. They are intentionally tracked as the unchecked P1 slices below
rather than as separate duplicate backlog rows: unauthenticated workspace
create/read surfaces, unauthenticated callback registration plus delivery-time
SSRF/DNS-rebinding risk, unauthenticated secret-lease inventory reads,
timing-unsafe bearer-token comparison, missing request admission/rate limiting,
and production configuration footguns.

- [x] Add API auth posture hardening and timing-safe token checks before GKE
  discussion. Acceptance: bearer-token validation uses constant-time comparison;
  intentionally public endpoints such as `/healthz` are explicitly annotated or
  covered by contract tests; sensitive reads/writes require auth by default or
  have a written local-dev exception; and workspace create/list/events behavior
  is re-evaluated for local-dev versus production/network-facing mode. Must
  explicitly cover `POST /v1/workspaces`, `GET /v1/workspaces`, workspace
  overview/get/events/stale-reasons, and
  `/v1/workspaces/{workspace_id}/secret-leases`; secret lease status may not
  expose secret names, mount targets, providers, or reference digests across an
  unauthenticated boundary. Tests should prove new routes are auth-required by
  default unless marked public in one place, and `require_api_token` should use
  `hmac.compare_digest` or equivalent constant-time comparison.
  Evidence: PR [#250](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/250)
  merged 2026-05-15 via monitor workspace `ws_c63623b7d5194bfa83cc702e`.
- [x] Harden callback registration and delivery against SSRF and DNS rebinding.
  Acceptance: callback registration/listing requires API auth; callback targets
  are revalidated at delivery time rather than only at registration; production
  policy can require HTTPS-only and optional allowlisted callback hosts; and
  tests verify callback envelopes remain minimal allowlisted event payloads
  rather than raw workspace internals. Must also close the current docs/code
  mismatch where `docs/REST_API_REFERENCE.md` says callback create/list require
  `Authorization: Bearer $AWF_API_TOKEN` but the route handlers do not enforce
  `require_api_token`.
  Evidence: PR [#249](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/249)
  merged 2026-05-15 via monitor workspace `ws_60589ae904754135b70e6e9f`.
- [x] Add bounded request admission for workspace creation and callback
  registration. Acceptance: workspace create and callback register endpoints
  enforce per-token or safe local fallback rate limits; burst exhaustion returns
  structured operator-visible reason codes/events; and idempotency replay cannot
  bypass the limiter or create duplicate expensive work. The limiter should be
  evaluated together with the auth posture slice: unauthenticated local-dev
  exceptions, if retained, still need bounded admission so repeated create or
  callback requests cannot exhaust Docker, Git mirrors, disk, or database rows.
  Evidence: PR [#256](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/256)
  merged 2026-05-16 via monitor workspace `ws_8b76839898f1400abc16ad08`.
- [x] Add production configuration footgun guardrails. Acceptance: local dev
  defaults remain usable, but production/network-facing mode fails fast when
  `AWF_DATABASE_URL` or security-sensitive callback/auth settings use bundled
  development defaults; docs explain local versus production expectations; and
  tests prove insecure production config is rejected. Must cover default
  `postgresql+asyncpg://awf:awf_dev@localhost:5433/awf`, callbacks enabled with
  insecure policy, and missing/weak API-token posture in production mode.
  Evidence: PR [#255](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/255)
  merged 2026-05-15 via clean retry workspace `ws_084580a1fa544b95bcbcab98`.
- [x] Complete low-risk security cleanup audit from the 2026-05-14 architecture
  review. Acceptance: fragile SQL interval string interpolation is replaced by a
  typed/parameterized SQLAlchemy expression; selected 409/error responses avoid
  unnecessary internal field leakage; and doctor known-secret sets are covered
  by tests proving they are used only for redaction and never emitted.
  Evidence: PR [#257](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/257)
  merged 2026-05-15 via workspace `ws_7bad4fd57a2b4995acc9292a`.
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
- [x] Add reusable restricted egress allowlist templates for common local
  engineering needs: GitHub/git remotes, configured model provider APIs,
  package registries such as PyPI/npm/uv indexes, OS package mirrors when
  declared, and documentation domains. New project onboarding should recommend
  restricted mode by default and explain when to choose open mode.
- [x] Add outbound egress audit evidence without leaking secrets: record
  workspace id, policy posture, destination host/category, allow/deny decision,
  timestamp, and reason code for policy-controlled network attempts; expose
  summary counts in service status, workspace detail, MCP, metrics, and console
  security panels. Evidence: workspace `ws_7e7f6d54bc924c47a5723621` / PR
  [#212](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/212)
  merged 2026-05-06 with redacted egress audit evidence across service,
  workspace, MCP, metrics, and console surfaces.
- [x] Add prompt-injection boundary controls for untrusted external content:
  GitHub comments, PR review text, issues, webpages, package READMEs, and CI
  logs must be passed to agents as quoted evidence with source provenance, not
  as authority over AWF/system/task policy. Add regression tests proving
  adversarial external text cannot override owned paths, validation policy,
  secret handling, merge gates, or cleanup rules.
- [x] Add supply-chain guardrails for agent-run package installation and remote
  script execution. Profiles should be able to choose warn/block modes for
  unpinned dependency installs, curl-pipe-shell patterns, unexpected registry
  hosts, and lockfile changes outside owned paths; violations should produce
  structured findings and operator-visible recovery guidance. Implemented in
  `ws_a1357eb1d1db498a9ed499ed`: `security.supply_chain` profile policy,
  structured `PolicyFinding` reason codes, executor and PR-monitor pre-commit /
  pre-push blocking, and focused regressions for warn, block, allowed, and
  false-positive-safe cases. PR [#197](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/197)
  merged 2026-05-05. Evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_supply_chain_policy.py tests/unit/profiles/test_security_policy.py tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_inline_profile_accepts_and_returns_supply_chain_policy tests/unit/control/test_executor_validation_fix_cycle.py::TestSupplyChainPolicy tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocking_supply_chain_finding_is_not_committed_or_pushed -q`;
  `uv run --python 3.12 --extra dev mypy src/awf`.

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
- [x] Prefer Codex default `gpt-5.5` / `xhigh` when a non-default Codex model
  fails with capacity/quota/usage-limit exhaustion and no explicit fallback
  policy is configured. Regression source: 2026-05-14 Spark-launched
  workspaces `ws_61e0f7b210fa423faef0b6f3`,
  `ws_7dd27492f4184baf8eb67b81`, and
  `ws_e56b535618c649cdb5a60999` reached PRs but stalled/failed after
  Codex `gpt-5.3-codex-spark` capacity/circuit failures. Acceptance:
  provider recovery must switch monitor/agent recovery to the Codex default
  model instead of repeatedly selecting an exhausted non-default model; it
  must not fall back from `gpt-5.5` to itself; and stale-active cleanup must
  not terminalize monitor rows that are waiting for provider recovery retry or
  fallback. Evidence: local 2026-05-15 fix adds provider-recovery and worker
  stale-active regression tests, rebuilds/restarts AWF, and re-adopts PRs
  #247/#249/#250 with Codex `gpt-5.5` monitors.
- [x] Clean up no-work failed containers, networks, and pressure directories
  after logs/artifacts are durably retained, without removing evidence needed
  for failure analysis or retries.
- [x] Add TDD coverage proving provider-capacity failures retry or fallback
  automatically, non-transient agent failures do not loop forever, and fallback
  attempts inherit validation, owned paths, profile, auto-merge, and monitor
  policy correctly.

## P0: Control-Plane Restart Recovery Hardening

- [x] When a restarted worker recovers a persisted `monitoring_pr` workspace,
  clear or expire irrelevant stale execution claims from the previous worker,
  preserve the active monitor claim, emit an explicit recovery event, and prove
  with regression tests that PR monitoring continues without duplicate monitor
  loops or misleading execution-capacity reservations.
- [x] Adopt or safely preserve running agent executions after worker restart
  or transient control-loop loss. Regression source:
  `ws_4f44c108a58f46d092f4e411` was failed as `STALE_ACTIVE_EXECUTION` even
  though the API and worker Docker containers had not restarted; the immediate
  control-plane symptom was repeated closed asyncpg connections and a missing
  in-memory execution task. Acceptance: if the control-plane worker restarts or
  temporarily loses DB/control-loop continuity while a workspace is in
  `running`/`validating`/`pushing` and Docker still has a live agent runtime,
  AWF must not automatically `compose down` that runtime as stale-active. It
  should either reattach/adopt the running execution with durable ownership and
  log continuation, or transition to a clear recoverable state that preserves
  the container, worktree, logs, and implementation diff for explicit retry or
  operator recovery. Regression coverage must prove a worker restart during an
  active agent run cannot kill five healthy workspaces merely because the new
  worker has an empty in-memory execution task map.
  Scope: make restart recovery distinguish truly orphaned stale-active
  resources from live agent/validation/push executions whose durable DB state
  still says active; persist enough execution/runtime identity to make the
  decision auditable; surface the recoverable/preserved state in events,
  operations, status, and runtime health; and add tests for single-workspace and
  multi-workspace restart scenarios, including no-container, live-container,
  expired-claim, active-claim, and cleanup-failure paths. Evidence: workspace
  `ws_13dd6ba7165141c285bd771e` / PR
  [#219](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/219)
  merged 2026-05-07 and preserves healthy live agent/validation/push
  executions instead of tearing them down after worker restart or in-memory
  execution-task loss.
- [x] **P0: Close preserved-active execution reattach gap after worker
  restart.** Regression source: `ws_0d8e6ceeb322430daa745ad6` was running an
  agent, completed implementation, passed focused validation, and committed
  locally, then a local service restart emitted
  `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART` but did not reattach the new
  worker to the execution. Stale-active cleanup later emitted
  `STALE_ACTIVE_EXECUTION`, stopped the runtime, and failed the workspace
  before PR creation. Acceptance: when AWF preserves a live active runtime after
  restart, it must either reattach/resume execution ownership, recover from a
  clean committed worktree into validation/push/PR creation, or automatically
  perform the same lineage-preserving fallback an operator would perform
  manually: if a branch/PR already exists, attach a fresh PR monitor with
  duplicate-monitor protection; if no usable PR/branch work exists, launch a
  replacement workspace with the same task, base, owned paths, provider/model,
  effort, resources, and auto-merge policy, while marking the original
  superseded with the root-cause link. Only unrecoverable ambiguity should leave
  an explicit operator-recoverable state, and that state must not automatically
  release the runtime as failed. Regression tests must cover restart after agent
  commit but before validation/push, restart after PR creation but before
  monitor handoff, and restart with no usable worktree changes; useful completed
  work cannot be terminalized without an automatic salvage/reschedule path.
  Completed by PR
  [#272](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/272),
  merged 2026-05-21 with fresh monitor workspace
  `ws_53702c4210de4ec59e9ec059`; cancelled stale monitor
  `ws_77bb4cce4aea4892bb41e0e6` is superseded.

## P0: API / CLI / MCP Contract Parity

- [x] Make workspace create/list surfaces parity-safe across REST, CLI, and MCP.
  Regression source: during the 2026-05-14 dogfood launch, the canonical REST
  `/v1/workspaces` request could select `task.model`, resources, and scheduler
  knobs, while `awf workspace create` could not express the same request without
  hand-written API calls. Separately, `awf workspace list --status requested
  --status provisioning --status ready --status running --status validating
  --status pushing --status monitoring_pr` returned zero rows even though direct
  `workspace show` proved active running workspaces existed, because REST/CLI
  accepted only one effective `status` filter. Acceptance: CLI create must expose
  canonical create fields already present in REST/MCP without CLI-only behavior;
  REST, CLI, and MCP list surfaces must support backward-compatible multi-status
  filtering for active-workspace queries; contract/parity tests must pin request
  fields, list filter semantics, docs status, and MCP schemas; and effort must be
  handled explicitly as either a canonical cross-surface request field or a
  documented profile/provider-derived value, not an accidental CLI gap.
  Evidence: `ws_f9c0654695334f2386c2c7eb` / PR
  [#246](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/246)
  merged 2026-05-14 after failed `ws_02ef6b49f7dc4657a8e63355` and superseded
  planning-scope failure `ws_b9112aecd2d94fc7b4babf26`; local commits `89ea11f`
  and `19c3ba99` fixed the restart/planning and transient fetch dogfood blockers
  before the clean retry merged.

## P1: API Contract Completion

- [x] Normalize pagination envelopes across list APIs.
- [x] Add explicit idempotency support to every mutating endpoint.
- [x] Add optimistic concurrency or version checks to mutating workspace/candidate operations.
- [x] Add callbacks/webhook support for external operators.
- [x] Add first-class operation endpoints for rebase, validate, refresh, and make-canonical.
- [x] Add artifact listing and download semantics beyond metadata.
- [x] Add failure analysis API with root cause, evidence links, and suggested recovery actions.
- [x] Collapse workspace creation to one canonical v1 API surface before public
  stabilization. The rich create contract lives at `POST /v1/workspaces`; the
  former duplicate create route and MCP tool are retired while AWF is still
  pre-stable. Public docs now describe one create surface and legacy operator
  scripts are no longer supported entrypoints.

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
- [x] Add launch-time LLM provider readiness preflight for workspace create and
  retry. Acceptance: AWF checks the selected agent/model provider before
  provisioning, reports exact provider/model/readiness/auth-source status
  through REST, CLI, MCP, console, and workspace events, blocks launch or
  requires an explicit override when required auth/model readiness is missing,
  and uses a real non-secret provider probe where file/env presence alone can be
  stale or non-portable, especially Claude Code OAuth and Gemini auth.
- [x] Research and decide the local control-plane container UID/GID strategy.
  Compare keeping API/worker as root with explicit post-provision ownership
  repair versus running local control-plane containers as the host UID/GID.
  Acceptance: document Docker socket, SSH/auth mounts, bind-mounted AWF state,
  linked worktree metadata, Linux/macOS behavior, cleanup permissions, and
  migration path for existing root-owned state; choose the default local setup
  and add regression coverage proving workspace containers can run `git status`,
  `git add`, and `git commit` in `/workspace`. Decision: keep the local control
  plane root with explicit post-provision chown to UID/GID 1000; see
  `docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md` and the regression tests in
  `tests/unit/node/test_git_manager.py::TestAgentWorktreeWritable` plus
  `tests/integration/test_workspace_agent_git_in_workspace.py`.
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
- [x] Define the primary install path: package-manager install such as `uv tool install aira-awf`/`uv pip install aira-awf`, with git clone as the contributor path.
- [x] Add a one-command local bootstrap such as `awf init` that checks Docker, writes local env defaults, creates the AWF state directory, starts or validates Postgres/API/worker/console, and prints next steps.
- [x] Add `awf doctor` or extend `awf service status` to diagnose missing Docker, auth, API token, GitHub CLI, provider credentials, ports, disk, and stale containers in plain language.
- [x] Add copy-paste onboarding prompts for Codex, Claude Code, Gemini, OpenCode, and OpenClaw: "inspect this project, generate `.awf/workspace.yml`, preview it, launch a smoke workspace, then implement feature X through AWF."
- [x] Add a smoke workspace command that can be run from any project after `awf init` to prove the local service, auth, profile, validation, PR creation, and console links work.
- [x] Publish an API/CLI/MCP parity matrix and treat missing MCP coverage as an explicit backlog item.
- [x] Convert the parity matrix into an implementation driver: every surface marked
  missing or partial must map to a concrete P1 implementation issue/slice, with
  REST endpoint, CLI command, MCP tool name, schema/error-code contract, and
  security boundary recorded. The matrix should not be considered complete if it
  only documents gaps without creating executable follow-up work.
- [x] Add MCP tools for merge queue, task attempts, validation provenance,
  stale reasons, artifacts, metrics, locks/overlap graph, and service
  health/status. Evidence: `src/awf/mcp/server.py` registers the read-only
  tools with bounded list inputs; `tests/unit/mcp/test_mcp_operator_surfaces.py`
  covers populated and empty REST-vs-MCP parity, structured error/null states,
  secret redaction, artifact metadata-only behavior, and route-handler bypass.
- [x] Add MCP tools for safe operator actions already present in the API: retry plus remonitor, refresh, validate, rebase, cancel, stop, and destroy. Remonitor, refresh, validate, rebase, cancel, stop, and destroy share the idempotency/concurrency contract; retry is intentionally handled differently in the MCP schema/docs as a fresh-attempt operation without the same idempotency/versioning semantics. Evidence: `src/awf/mcp/server.py` registers `awf_retry_workspace`, `awf_refresh_workspace`, and `awf_rebase_workspace`, requires `idempotency_key` on the 7 idempotent/versioned control tools, and exposes optional `expected_version` (If-Match parity) on that set; `src/awf/service/workspaces.py` exposes `request_refresh_workspace` and `request_rebase_workspace` façade methods; contract tests in `tests/unit/mcp/test_mcp_control_contracts.py` cover success paths, replay/idempotency, version conflict, invalid-state errors, and structured error mapping; `tests/unit/mcp/test_mcp_server.py` covers schema contracts.
- [x] Add first-class AWF PR monitor adoption for existing GitHub PRs. Acceptance:
  an operator can provide `repo_url`/repo slug plus PR number or URL, and AWF
  creates or attaches a service-managed workspace/merge candidate in
  `monitoring_pr` without rerunning the original coding agent. This must be
  exposed through REST, CLI, and MCP; must be idempotent per repo/PR; must
  reject closed/merged PRs with structured reason codes; must support
  `auto_merge` versus manual monitor policy; must record task/attempt lineage,
  PR URL, head/base refs, validation freshness state, and durable monitor logs;
  and must keep Core users on one supported adoption flow through REST, CLI,
  and MCP.
  Evidence: implemented `POST /v1/workspaces/adopt-pr`,
  `awf workspace adopt-pr`, and MCP tool `awf_adopt_pull_request_monitor`;
  adoption persists task/attempt lineage, queue/resource records, an `adopt_pr`
  operation, PR metadata, validation freshness, monitor log links, and
  deterministic repo/PR idempotency; executor/provisioner tests cover
  no-agent/no-new-PR monitor handoff for `sync_feature_pr`. Iteration 1
  conformance evidence: added executor/LogStore monitor-start and redaction
  coverage plus MCP terminal error-result coverage; on 2026-05-03, `ruff check
  src/awf tests/unit scripts`, `mypy src/awf`, the focused
  adoption/touched-file suite, and the service/api/cli/mcp/control/node/runtime
  unit gate all passed. PR
  [#198](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/198)
  merged 2026-05-05.
- [x] Align CLI command coverage with the canonical REST API and MCP surfaces:
  for each safe read/control operation, either expose the corresponding CLI
  command with the same auth/idempotency/concurrency/error semantics, or document
  why that surface is intentionally MCP/API-only.
  Evidence: implemented `awf workspace cancel`, `awf workspace stop`,
  `awf workspace destroy`, `awf workspace refresh`, `awf workspace validate`, and
  `awf workspace rebase`, plus global safe-read `awf operations list` and
  `awf operations show`, in `src/awf/cli/main.py`; added command presence/request
  shape/output shape/error-shape coverage in `tests/unit/cli/test_cli.py`; added a
  dedicated control-surface contract matrix and error-shape suite in
  `tests/unit/contracts/test_control_surface_parity_contract.py`; and
  `tests/unit/mcp/test_mcp_client_parity_docs.py` to pin parity documentation
  for intentional control-surface gaps.
  Iteration 3 landed in PR
  [#206](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/206).
  Focused artifacts are collected with:
  `tests/unit/cli/test_cli.py`, `tests/unit/contracts/test_control_surface_parity_contract.py`,
  `tests/unit/mcp/test_mcp_client_parity_docs.py`, `tests/unit/mcp/test_mcp_parity_matrix_crossref.py`,
  `tests/unit/mcp/test_mcp_operator_surfaces.py`, `tests/unit/api/test_controls.py`,
  `tests/unit/api/test_workspace_controls_idempotency.py`.
- [x] Add contract tests proving REST API, CLI, and MCP stay aligned: request
  payloads, response payloads, reason codes, idempotency keys, `If-Match` /
  workspace-version concurrency, auth failures, and structured error semantics
  must not drift across the three clients.
  Scope: extend the existing contract capability registry into executable
  parity checks for every implemented safe read/control surface; compare REST
  routes, CLI commands, MCP tool schemas, request field names, response envelope
  fields, public reason codes, idempotency-key requirements, optimistic
  concurrency/version semantics, auth failure shapes, and terminal structured
  errors. If a surface is intentionally API/MCP-only or still partial, the test
  must require an explicit matrix/backlog status instead of silently skipping
  it. Any real drift discovered by the tests should be fixed in the smallest
  compatible way. Evidence: `tests/unit/contracts/_capabilities.py` now covers
  implemented safe read/control REST, CLI, and MCP surfaces from
  `docs/MCP_CLIENT_PARITY.md`; `tests/unit/contracts/_introspection.py` and
  `test_surface_metadata_alignment.py` introspect real FastAPI routes, Typer
  commands, and MCP tool schemas; the contract suite covers request/response
  fields, reason codes, idempotency, If-Match/version behavior, auth failure
  shape, structured errors, explicit CLI absence, and MCP safety boundaries.
  The tests exposed MCP control idempotency drift, fixed by requiring
  `idempotency_key` on all seven idempotent/versioned MCP control tools.
  Validation: `pytest tests/unit/contracts -q` passed 189 tests and the
  focused API/CLI/MCP parity suite passed 343 tests; final iteration
  `ws_3a9bb03983e343e28f462e3e` / PR
  [#218](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/218)
  merged 2026-05-08 after rebase onto the current target branch and full
  coverage validation.
- [x] TODO§P1-operation-read-auth: Close REST auth parity for workspace and
  global operation read endpoints, or keep the parity matrix operation rows
  explicitly marked `MCP partial` until those REST surfaces require the same
  token boundary as the MCP tools. Evidence:
  `ws_94e4afca890d47b584208bfc` / PR
  [#244](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/244)
  merged 2026-05-14 with Gemini and focused REST auth parity coverage.
- [x] TODO§P1-artifact-download: Add a bounded MCP artifact content/download
  tool, or keep the matrix entry explicitly marked `MCP missing/backlog` until
  REST-compatible path validation, authorization, size limits, and error
  envelopes are covered. Evidence: `ws_01349ca4ecca408baff1d446` / PR
  [#238](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/238)
  merged 2026-05-13 and adds the bounded MCP artifact content read surface with
  size/path/error guardrails and reason-catalog coverage.
- [x] TODO§P1-mcp-global-events: Add MCP parity for the global
  `GET /v1/events` surface, or keep the workspace-events row explicitly
  marked `MCP partial` until global events have a real MCP tool and coverage.
  Completed by `ws_32a3971e4aa147c08ed46683` after failed
  `ws_cd0ccbb17db943ed8415aff1` and auto-retry
  `ws_dabd5b60a8464f10b927f1d2`; local commit `89ea11f` fixed the
  planning-scope retry prompt poisoning before relaunch.
- [x] TODO§create-parity: Add full MCP and CLI parity for `awf_create_workspace` and `workspace create` so callers can configure canonical create `out_of_scope_changes` and `provider_recovery` policies. Completed by `ws_61e0f7b210fa423faef0b6f3` and replacement monitor `ws_4599ede79dce445790f4c6e4`. Evidence: `docs/awf-plans/ws_61e0f7b210fa423faef0b6f3.validation.txt` and parity-coverage updates in `tests/unit/contracts`, `tests/unit/mcp/test_mcp_server.py`, and `tests/unit/cli/test_cli.py`.
- [x] Add a docs/status consistency test for the parity matrix so entries marked
  implemented must correspond to real REST routes, CLI commands, MCP tools, and
  contract-test coverage; partial or missing entries must remain visible as
  unchecked backlog work. Evidence: parity-matrix consistency coverage in
  `tests/unit/mcp/test_mcp_client_parity_docs.py`,
  `tests/unit/mcp/test_mcp_parity_matrix_crossref.py`, and
  `tests/unit/contracts/test_registry_smoke.py` validates implemented rows
  against the FastAPI route tree, Typer command tree, MCP tool registrations,
  active backlog visibility, and executable contract/parity coverage references.
  Validation: targeted parity/registry pytest, ruff on touched parity and
  contract helpers, and the full `tests/unit/contracts` suite passed on
  2026-05-05; evidence attached in
  `docs/awf-plans/ws_aeec0296eee64c869d328ae2.validation.txt`; PR
  [#215](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/215)
  completed 2026-05-06.
- [x] Keep MCP read/control scoped: expose AWF-managed runtime snapshots, logs,
  operations, and controls, but do not expose arbitrary shell or unrestricted
  Docker exec. Evidence: MCP operator-surface tests reject shell/exec/Docker
  exec/host-file/secret/artifact-content tool names and inputs, require bounded
  list/read schemas, and assert service health/readiness/resource summaries do
  not echo token values.
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

- [x] Add a top-level "Start Here" quickstart that gets a fresh evaluator from
  clone/install to meaningful AWF proof in under five minutes. Acceptance:
  three commands or fewer for the recommended path, expected output snippets,
  prerequisites called out before the first command, and links to deeper docs
  only after the first successful proof.
- [x] Split the README into a short product landing plus focused docs:
  getting started, concepts, CLI reference, REST API reference, MCP reference,
  troubleshooting, trust model, and contributor guide. Acceptance: README
  remains scannable under roughly 300 lines and points to one canonical doc for
  each developer journey stage.
- [x] Add an executable first-run DX smoke command, such as `awf demo run` or
  `awf smoke run`, that prints a step-by-step report for service readiness,
  profile preview, workspace request, validation evidence, PR/monitor evidence
  or mocked-local equivalent, and cleanup evidence. Acceptance: it is safe to
  run repeatedly, works without live GitHub when using mocked-local mode, and
  produces clear next actions on failure.
- [x] Publish a stable OpenAPI artifact and API examples for the Core control
  plane. Acceptance: `openapi.json` can be generated in CI, linked from docs,
  and paired with copy-paste `curl` examples for create workspace, list status,
  read logs/events, request validation, remonitor, retry, and release
  readiness. Evidence: `openapi.json` checked in at repo root, generated by
  `scripts/generate_openapi.py` and verified with `--check` drift mode;
  `tests/unit/api/test_openapi_artifact.py` covers spec generation, OpenAPI 3.x
  structural validation, path/method coverage, unique operation IDs, schema
  completeness, and JSON round-trip; `tests/unit/api/test_docs_drift.py` covers
  path-prefix presence in docs, curl-path validity against spec, and spec
  consistency with the checked-in artifact; `docs/REST_API_REFERENCE.md` expanded
  with copy-paste curl examples for all endpoint groups; AGENTS.md updated with
  the spec drift check command; `pyproject.toml` updated with
  `openapi-spec-validator>=0.7.0` dev dependency.
- [x] Document and demo PR monitor adoption for an already-open PR. Acceptance:
  the quickstart and API/CLI/MCP docs show the supported command/API call,
  required GitHub auth, idempotency behavior, monitor policy choice, and how to
  inspect adopted monitor logs/events/merge-queue state from the console.
  Scope: document the operator path for adopting an existing GitHub PR without
  rerunning the coding agent, including CLI, REST, and MCP examples; required
  GitHub token/permission checks; `auto_merge` versus manual monitor policy;
  deterministic repo/PR idempotency and terminal-row retry behavior; console
  inspection of logs, events, validation provenance, merge queue, and recovery
  operations; and a mocked-local or docs-tested demo path that can be validated
  without a live PR. Evidence: added `docs/PR_MONITOR_ADOPTION.md` as the
  canonical operator runbook; linked it from README, quickstart,
  getting-started, client-surface, REST, CLI, and MCP docs; expanded
  `docs/REST_API_REFERENCE.md` to remove the incorrect adoption
  `Idempotency-Key` requirement and show inspection/recovery API calls; added
  `awf_adopt_pull_request_monitor` examples to `docs/MCP_REFERENCE.md`; and
  added `tests/unit/docs/test_pr_monitor_adoption_docs.py` to verify real
  REST/CLI/MCP names, auth/token guidance, monitor policy, deterministic
  idempotency, current terminal-row behavior, console/API/CLI/MCP inspection,
  recovery tools, and mocked-local test references. Validation: docs adoption
  contract tests passed; focused REST drift/adoption, CLI adoption, MCP parity,
  MCP adoption, and request-payload alignment tests passed; ruff passed for
  touched docs tests; `uv run --python 3.12 --extra dev python
  scripts/generate_openapi.py --check` passed.
- [x] Decide the SDK stance before open-source Core release: either ship a
  minimal Python client for the stable operator flows or explicitly document
  that REST + CLI + MCP are the supported client surfaces for v0.1. Acceptance:
  the decision is reflected in README, API docs, and the parity matrix so
  integrators do not write against accidental internal modules.
- [x] Add a searchable reason-code and error-code catalog. Acceptance: common
  API/CLI/MCP failures include problem, likely cause, operator fix, related
  command, and docs link; release readiness fails if new public reason codes
  lack catalog coverage.
- [x] Improve CLI help text for first-time users. Acceptance: `awf --help`,
  `awf init --help`, `awf service bootstrap --help`, and workspace commands
  explain the recommended first path, safety defaults, dry-run behavior, and
  whether the command mutates local state, Docker, GitHub, or Git branches.
- [x] Add a troubleshooting guide organized by first-run failure symptom:
  Docker unavailable, Postgres unavailable, GitHub auth missing, provider auth
  missing, package install failure, disk pressure, port conflict, provider
  outage, stale PR monitor, and cleanup/orphan warning. Acceptance: every item
  includes the exact command to diagnose and the safest recovery command.
  Evidence: `ws_91940abf598341b789390979` / PR
  [#243](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/243)
  merged 2026-05-14.
- [x] Add docs search/readability checks for public docs. Acceptance: CI or a
  docs-status test confirms every public guide is linked from the docs index,
  key commands still exist in CLI help, and snippets marked copy-paste are
  syntactically valid. Evidence: `ws_0bc1d8f718bc480998d0a08d` / PR
  [#217](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/217)
  completed 2026-05-06.
- [x] Add a "first-time evaluator" telemetry-free feedback loop for local Core,
  such as a generated `awf doctor --bundle` redacted support artifact or a
  GitHub issue template path from failed readiness output. Acceptance: no
  secrets are included, and developers can file useful issues without manually
  collecting ten logs.

## P1: Operator Console Completion

- [x] Show exact agent model and thinking/effort settings for every workspace.
- [x] Add console filters for all supported agents, including OpenCode, and an
  exact model filter so operators can view workspaces by provider/model while
  defaulting to all workspaces.
- [x] Show lifecycle stage start time, end time, and duration.
- [x] Show validation tier, validation freshness, command hash, and target SHA.
- [x] Show token usage when providers expose it.
- [x] Show estimated cost only when reliable pricing metadata is configured.
- [x] Add merge queue blocker drill-down.
- [x] Add stale reason and recovery action drill-down.
- [x] Add safe remonitor/refresh/revalidate controls after API hardening.
- [x] Add live workspace activity signals such as `last_activity_at`,
  `last_log_at`, active agent/conformance/validation subphase, and stale-running
  warnings so operators can distinguish a genuinely working agent from a
  stuck `running` workspace whose row `updated_at` has not changed. Evidence:
  workspace `ws_ce68a96b836442eb96a1255a` / PR
  [#208](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/208)
  merged 2026-05-05 and supersedes the failed `ws_681eec29b3e44a0daa4a0264`
  attempt for this slice.
- [x] Add security/secret/egress status panels.
- [x] Add a polished dark theme and accessibility controls for the web console,
  including larger font-size options, high-contrast mode, preserved operator
  preference, keyboard/focus-visible coverage, and browser-verified responsive
  screenshots for the main dashboard, workspace inspector, logs, and merge
  queue.
- [x] Restructure the wide-screen console so global dashboard panes stay
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
- [x] Retire legacy operator scripts once the API-backed runner fully replaces
  them. The supported operator path is now the service-backed REST/CLI/MCP
  surface; only OpenAPI and reason-catalog generator scripts remain under
  `scripts/`.
- [x] Auto-prune git worktrees for completed and merged workspaces. AWF should
  detect terminal workspaces whose PR has landed, preserve retained
  logs/artifacts/audit metadata, release reservations, and safely prune linked
  git worktrees after the configured retention window or explicit operator
  policy. Add dry-run evidence through cleanup/doctor/status surfaces and
  regression tests proving active, failed-preserved, and unmerged PR worktrees
  are never pruned.

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
- [ ] Design indexed host-port admission state before high workspace counts:
  replace the current JSON/Python full-scan conflict detection with either
  indexed JSONB predicates or a denormalized `host_ports` integer-array field
  with a GIN index, and add the supporting workspace-event index needed by
  terminal-runtime release checks.
- [ ] Define Helm or Kustomize deployment package.
- [ ] Define production logging, metrics, traces, and alerting.
- [ ] Decompose monolithic executor, PR monitor runner, worker, and repository
  modules after local Core P0/P1 readiness is stable. Deferral rationale: the
  2026-05-14 architecture review correctly identified maintainability risk in
  multi-thousand-line modules, but broad decomposition is technical debt rather
  than a pre-GKE local-readiness blocker and would add avoidable churn while
  reliability/security P0/P1 slices are still active.

## Ready For GKE Discussion When

- [x] All P0 items are complete, including active AWF dogfood slices and the
  umbrella provider-recovery acceptance item.
- [x] All P1 items are complete, or explicitly deferred with a written reason
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
