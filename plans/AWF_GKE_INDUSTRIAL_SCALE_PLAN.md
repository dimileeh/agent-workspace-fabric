# AWF GKE Industrial Scale Enhancement Plan

## Problem Statement

`docs/AWF_ON_GKE_PRD.md` defined the first private GKE architecture, but the
target now needs to be explicit about thousands of customers, thousands of
repositories, and hundreds of thousands of active workspaces. The document must
describe the system as an industrial SaaS control plane, not a single-cluster
cloud port of local AWF Core.

## Scope

- Review the existing PRD through `/plan-eng-review`.
- Verify current GKE/Kubernetes scale constraints and best practices from
  primary docs.
- Enhance the PRD with cell-based fleet architecture, scheduler/backpressure,
  data-store split, repo-cache scale, PR monitor scale, object budgets,
  observability cardinality, failure modes, tests, rollout, and SLOs.

## Out Of Scope

- Implementing GKE code, Terraform, Helm, controllers, or schemas.
- Choosing final GKE Standard versus Autopilot.
- Choosing final Spanner versus sharded Postgres for the first beta.
- Linking the new PRD from public docs before the open-source release boundary
  is decided.

## Requirements Checklist

- [ ] Define industrial scale targets.
- [ ] Replace single-cluster assumptions with cell/fleet architecture.
- [ ] Add Kubernetes object budget and resident/ephemeral workload split.
- [ ] Add store split for hot state, events, audit, artifacts, and usage.
- [ ] Add two-level scheduling, quotas, backpressure, and cost controls.
- [ ] Add repo cache and Git provider rate-limit design.
- [ ] Add webhook-first PR monitor and merge queue scale design.
- [ ] Add BYOK key isolation and egress enforcement architecture.
- [ ] Add observability cardinality rules and cell health model.
- [ ] Add failure modes, scale test coverage diagram, SLOs, rollout, and risks.

## Verification

- Structural heading scan.
- Keyword coverage scan for industrial-scale concepts.
- ASCII-only scan for edited docs.
- Manual consistency check against GKE/Kubernetes primary documentation.
