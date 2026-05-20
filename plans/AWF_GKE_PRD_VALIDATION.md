# AWF GKE PRD Validation

Plan reference: `plans/AWF_GKE_PRD_PLAN.md`
Document validated: `docs/AWF_ON_GKE_PRD.md`
Date: 2026-05-18

## Summary

The AWF-on-GKE PRD and architecture document was created as a documentation-only
artifact. It recommends a private `awf-cloud` repo that consumes the public AWF
Core, defines the three product offerings, and specifies org -> project -> user
tenancy, GKE workspace execution, secrets, network policy, observability,
rollout, and acceptance gates.

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Recommend whether GKE code should live here or in a separate repo | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Executive Decision", "User Concerns Answered", and "Implementation Alternatives" recommend a separate private cloud repo. |
| Define open-source, BYOK, and full SaaS modes | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Executive Decision" and "Product Surface" define all three offerings. |
| Define org -> project -> user auth boundaries | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Tenancy And Identity", "Authentication", and "Authorization". |
| Define GKE topology and workspace execution | Complete | `docs/AWF_ON_GKE_PRD.md` sections "System Architecture", "GKE Isolation Model", and "Workspace Runtime On GKE". |
| Define secret, GitHub credential, and model-provider key handling | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Secrets And Credentials", "Security Threat Model", and BYOK offering definition. |
| Define isolation boundaries | Complete | `docs/AWF_ON_GKE_PRD.md` sections "GKE Isolation Model", "Network Policy", and "Data Model Additions". |
| Define observability, audit, cost controls, and operations | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Scheduling, Quotas, And Cost Controls", "Observability And Operations", and "Observability SLOs". |
| Include phased delivery, acceptance criteria, risks, and non-goals | Complete | `docs/AWF_ON_GKE_PRD.md` sections "Delivery Phases", "Acceptance Criteria", "Risks And Mitigations", "Non-Goals", and "NOT In Scope For First GKE Phase". |

## Source Consistency Check

| Source | Alignment |
| --- | --- |
| `docs/awf_prd_v2.2.md` | Preserves Layer 2A control plane / Layer 2B execution plane split, validation tiers, state model, secret leases, egress modes, and GCP/GKE production direction. |
| `TODO/pre-gke-industrial-readiness.md` | Treats GKE as P2, keeps implementation blocked until local Core P0/P1 readiness gates are complete or explicitly deferred. |
| `README.md` | Preserves current status: local Core is alpha and hosted/GKE/multi-tenant deployments are future layers. |
| `.awf/workspace.yml` | Preserves Core profile-driven services, validation, egress posture, and local trust model as reusable concepts for cloud mapping. |
| GKE/Kubernetes docs | Reflects namespace, RBAC, NetworkPolicy, ResourceQuota, and Workload Identity as layered isolation mechanisms rather than assuming Kubernetes gives perfect multi-tenancy by default. |

## User Concern Validation

1. Open-source boundary: Complete. The PRD explicitly keeps Helm, Terraform,
   tenant auth, billing, and production GKE topology out of the public repo.
2. Organization/project/user isolation: Complete. The PRD defines tenant-scoped
   entities, roles, actor types, authorization rules, and GKE namespace/runtime
   isolation.
3. Separate repo: Complete. The PRD recommends a private `awf-cloud` or
   `aira-awf-cloud` repo that consumes Core through pinned package/image
   versions and contract tests.

## Verification Commands

```bash
rg -n "^(#|##|###) " docs/AWF_ON_GKE_PRD.md plans/AWF_GKE_PRD_PLAN.md
rg -n "open-source|BYOK|Full SaaS|org_id|project_id|GKE|separate private repo|Workload Identity|NetworkPolicy|ResourceQuota|Helm|Terraform|tenant" docs/AWF_ON_GKE_PRD.md
LC_ALL=C rg -n "[^\\x00-\\x7F]" docs/AWF_ON_GKE_PRD.md plans/AWF_GKE_PRD_PLAN.md || true
wc -l docs/AWF_ON_GKE_PRD.md plans/AWF_GKE_PRD_PLAN.md
```

Results:

- Headings and required concepts were present.
- No non-ASCII characters were found in the new plan or PRD files.
- `docs/AWF_ON_GKE_PRD.md` has 1029 lines.
- `plans/AWF_GKE_PRD_PLAN.md` has 61 lines.

## Remaining Gaps

No planned documentation requirements are missing. The PRD intentionally leaves
five product/architecture decisions open for the implementation planning phase:
BYOK GitHub ownership model, GKE Standard versus Autopilot, direct Kubernetes API
versus CRD, first customer isolation tier, and whether generic `tenant_scope`
lands in public Core before the cloud MVP.
