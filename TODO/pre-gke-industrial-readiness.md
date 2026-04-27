# AWF Pre-GKE Industrial Readiness Checklist

Last updated: 2026-04-28

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
- Update this file whenever a PR lands that materially completes an item.
- Prefer AWF dogfood delivery with `auto_merge=true` for non-`main` targets.
- Keep TDD mandatory and keep AWF self-development coverage at 99%+.

Priority key:

- P0: required before GKE design starts.
- P1: required before a credible GKE pilot.
- P2: can be planned during or after the first GKE pilot.

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

- [ ] Ensure PR monitor never re-enters the full agent execution path for validate-only, rebase-only, or recovery-only work.
- [ ] Add end-to-end regression coverage for `monitoring_pr -> ready -> running` regressions after PR creation.
- [ ] Prove manual-merge mode waits until the human merge is observed, then completes and cleans up.
- [ ] Prove auto-merge mode waits for grace, comments, checks, freshness, validation tier, and final settle recheck.
- [ ] Prove transient GitHub errors are retried without losing monitor state.
- [ ] Prove non-actionable bot comments are ignored without suppressing meaningful later comments.
- [ ] Add explicit monitor recovery operation records for rebase, validate-only, remonitor, and human wait.
- [ ] Make PR monitor state transitions visible in the console as operations, not just log text.

## P0: Stale Detection And Merge Queue Truth

- [ ] Make target branch monitor detect every merged PR that can stale open candidates.
- [ ] Mark candidates stale when target branch advances and validation freshness is invalid.
- [ ] Treat owned-path overlap as advisory at launch time, not blocking.
- [ ] Use overlap as a stale-risk input after another candidate lands.
- [ ] Detect dependency/build config changes as structured stale reasons.
- [ ] Detect migration/schema/model changes for migration-sensitive tasks.
- [ ] Keep stale reasons active until a successful refresh/rebase plus required validation clears them.
- [ ] Make `/v1/merge-queue` candidate-backed readiness the single source of truth.
- [ ] Display candidate blockers, stale reasons, required action, and canonical attempt in the console.

## P0: Validation Tier Provenance As Merge Policy

- [ ] Define the freshness identity for every validation run:
  command set hash, target branch, target SHA, base SHA, profile version, and environment identity.
- [ ] Enforce Tier 1 as the normal profile/request validation gate.
- [ ] Enforce Tier 2 after rebase, stale refresh, conflict resolution, or target branch drift.
- [ ] Represent Tier 3 metadata and policy before full Tier 3 infrastructure exists.
- [ ] Store validation log stream references for every validation run.
- [ ] Ensure merge eligibility reads `validation_runs`, not old operation rows or log-derived state.
- [ ] Expose required tier, latest satisfied tier, validation freshness, and reason code in API and console.
- [ ] Prevent agents from lowering coverage thresholds, profile requirements, or PRD quality gates.

## P0: Operation And Recovery Truth

- [ ] Make cancel, stop, delete, remonitor, refresh, rebase, and validate idempotent operations.
- [ ] Add public API endpoints for refresh, rebase, and validate operations.
- [ ] Add optimistic concurrency or equivalent conflict protection for mutating APIs.
- [ ] Persist operation start, finish, owner, reason, result, failure code, and log streams.
- [ ] Ensure cancelled/destroyed workspaces cannot move forward after stale executor or monitor callbacks.
- [ ] Add recovery for stranded workspaces whose containers exited but DB state is active.
- [ ] Add recovery for active PR workspaces after AWF service restart.
- [ ] Add console controls for safe remonitor/refresh/revalidate once API semantics are stable.

## P0: Reliability, Cleanup, And SLOs

- [ ] Define and expose rolling creation success, cleanup success, stuck-state, and recovery success metrics.
- [ ] Add stuck-state watchdog metrics and actionable reason codes.
- [ ] Detect orphan containers, networks, volumes, and worktrees.
- [ ] Automatically clean completed PR workspaces after merge and safe retention.
- [ ] Preserve logs/artifacts during cleanup according to retention policy.
- [ ] Make cleanup idempotent and safe after partial Docker failures.
- [ ] Add SLO-style API and console indicators for local AWF health.
- [ ] Keep local disk pressure and admission blocking actionable in service status.

## P0: Test Coverage And Quality Gates

- [ ] Keep branch coverage enabled.
- [ ] Keep AWF self-development coverage at 99%+.
- [ ] Add coverage reports that explain remaining gaps instead of only failing a threshold.
- [ ] Add focused tests for PR monitor recovery, stale detection, validation tier gating, and service restart recovery.
- [ ] Add integration tests for two parallel PRs where one merge stales the other.
- [ ] Add integration tests for Alembic multi-head detection and automatic merge revision generation.
- [ ] Add integration tests for Dockerized project profiles with sidecar services.
- [ ] Forbid empty tests, fake assertions, and broad monkeypatching that skips behavior under test.

## P1: Security, Secrets, And Egress Policy

- [ ] Replace broad static auth mounts with declared secret leases where possible.
- [ ] Track secret lease issue, mount, expiry, revoke, and audit events.
- [ ] Revoke workspace secrets when workspace reaches terminal cleanup.
- [ ] Redact known token patterns from persisted logs and artifacts.
- [ ] Add profile lint failures for unsafe secret targets and broad host-home mounts.
- [ ] Enforce egress policy at Docker network/profile level in local mode.
- [ ] Add provider-specific least-privilege credential checks for Codex, Claude, Gemini, OpenCode/Ollama, GitHub, and Docker.
- [ ] Add audit trails for PR creation, push, merge, comment resolution, and destructive operations.

## P1: Workspace Services And Realistic Project Profiles

- [ ] Strengthen Docker Compose profile execution inside per-workspace DinD.
- [ ] Add integration fixtures for Python service plus Postgres.
- [ ] Add integration fixtures for Node/Next.js plus browser/Playwright validation.
- [ ] Add Redis/app/worker/service sidecar examples.
- [ ] Add health-check wait semantics before validation.
- [ ] Add profile-defined app endpoints exposed to agents and validation commands.
- [ ] Add database refresh/generation hooks for DB-backed profiles.
- [ ] Add migration-chain validation for Python/Alembic workloads.

## P1: Scheduler, Reservations, And Advisory Overlap Graph

- [ ] Keep workspace/task submission non-blocking when owned paths overlap.
- [ ] Add an operator-visible overlap graph for running and queued workspaces.
- [ ] Use overlap graph to warn agents in prompts and stale policy, not to prevent parallel work.
- [ ] Finish resource reservation accounting for CPU, memory, disk, and DinD pressure.
- [ ] Add fairness and starvation prevention for long-lived queues.
- [ ] Add task class bias and priority scoring as described in the PRD.
- [ ] Add human-escalation boost and retry-aware queue scoring.
- [ ] Make scheduler decisions visible as durable records and console explanations.

## P1: API Contract Completion

- [ ] Normalize pagination envelopes across list APIs.
- [ ] Add explicit idempotency support to every mutating endpoint.
- [ ] Add optimistic concurrency or version checks to mutating workspace/candidate operations.
- [ ] Add callbacks/webhook support for external operators.
- [ ] Add first-class operation endpoints for rebase, validate, refresh, and make-canonical.
- [ ] Add artifact listing and download semantics beyond metadata.
- [ ] Add failure analysis API with root cause, evidence links, and suggested recovery actions.
- [ ] Keep old compatibility endpoints stable until a documented v2 API cutover.

## P1: Operator Console Completion

- [ ] Show exact agent model and thinking/effort settings for every workspace.
- [ ] Show lifecycle stage start time, end time, and duration.
- [ ] Show validation tier, validation freshness, command hash, and target SHA.
- [ ] Show token usage when providers expose it.
- [ ] Show estimated cost only when reliable pricing metadata is configured.
- [ ] Add merge queue blocker drill-down.
- [ ] Add stale reason and recovery action drill-down.
- [ ] Add safe remonitor/refresh/revalidate controls after API hardening.
- [ ] Add security/secret/egress status panels.

## P1: Local Packaging And Upgrade Path

- [ ] Make local service bootstrap one-command and repeatable.
- [ ] Make migrations run safely during service startup or documented bootstrap.
- [ ] Add image versioning and local upgrade notes.
- [ ] Add backup/restore instructions for AWF control-plane Postgres.
- [ ] Add local disaster recovery instructions for stuck containers, broken migrations, and corrupt work dirs.
- [ ] Keep `scripts/run_awf.py` compatibility documented until the API-backed runner fully replaces it.

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
- [ ] The console can explain every blocked workspace without reading raw logs.
- [ ] AWF self-development passes 99%+ coverage with meaningful tests.
- [ ] A Dockerized toy project with DB, app, and browser validation passes end to end.

