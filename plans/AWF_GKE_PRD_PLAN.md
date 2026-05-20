# AWF GKE PRD Plan

## Problem Statement

AWF Core is preparing for open-source local release, while the next product
phase needs a private GKE-backed architecture for hosted and commercial
offerings. The plan must resolve the boundary between open-source Core and
private GKE/SaaS code, define organization/project/user isolation, and decide
whether the GKE service belongs in this repo or a separate repo.

## Scope

- Read `TODO/pre-gke-industrial-readiness.md` and `docs/awf_prd_v2.2.md`.
- Preserve AWF Core as a generic open-source local control plane.
- Define a private AWF-on-GKE architecture that consumes Core without forking it.
- Define product packaging for open-source, BYOK, and full SaaS modes.
- Define tenancy, authentication, authorization, runtime isolation, data model,
  observability, rollout phases, and acceptance criteria.
- Write the resulting PRD/architecture document as `docs/AWF_ON_GKE_PRD.md`.
- Validate the document against the user's three concerns and the source docs.

## Out Of Scope

- Implementing Kubernetes controllers, Helm charts, Terraform, or production
  auth code.
- Moving existing AWF modules between repositories.
- Changing public APIs or database schemas in this pass.
- Starting GKE implementation before local Core P0/P1 readiness gates are met.

## Requirements Checklist

- [ ] Recommend whether GKE code should live in this repository or a separate
  repo, with rationale.
- [ ] Define how open-source, BYOK, and full SaaS offerings differ.
- [ ] Define org -> project -> user authentication and authorization boundaries.
- [ ] Define GKE deployment topology and workspace execution model.
- [ ] Define secret, GitHub credential, and model-provider key handling.
- [ ] Define isolation boundaries for control plane, tenant data, and workspace
  runtime.
- [ ] Define observability, audit, cost controls, and production operations.
- [ ] Include phased delivery, acceptance criteria, risks, and explicit
  non-goals.

## Implementation Steps

1. Audit repo status and source docs.
2. Synthesize alternatives and choose the architecture direction.
3. Draft `docs/AWF_ON_GKE_PRD.md`.
4. Validate the document against the requirements checklist.
5. Record validation in `plans/AWF_GKE_PRD_VALIDATION.md`.

## Verification

- Manual source-doc consistency check against:
  - `docs/awf_prd_v2.2.md`
  - `TODO/pre-gke-industrial-readiness.md`
  - `README.md`
  - `.awf/workspace.yml`
- Confirm the PRD addresses all three user concerns.
- Confirm the PRD does not prescribe open-sourcing proprietary GKE deployment
  assets.
