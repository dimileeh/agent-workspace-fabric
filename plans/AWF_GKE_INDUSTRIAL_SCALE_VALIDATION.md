# AWF GKE Industrial Scale Validation

Plan reference: `plans/AWF_GKE_INDUSTRIAL_SCALE_PLAN.md`
Document validated: `docs/AWF_ON_GKE_PRD.md`
Date: 2026-05-18

## Summary

The PRD was enhanced from a first-cloud architecture into a fleet-scale SaaS
architecture. It now explicitly targets thousands of organizations, thousands
of repos, and 100,000-500,000 active workspaces across execution cells.

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Define industrial scale targets | Complete | `Industrial Scale Target` section. |
| Replace single-cluster assumptions with cell/fleet architecture | Complete | `System Architecture`, `Cell And Cluster Sharding`, `Regional And Fleet Topology`. |
| Add Kubernetes object budget and resident/ephemeral split | Complete | `Kubernetes Object Budget`, `Resident versus ephemeral workload split`. |
| Add store split | Complete | `Data Model Additions`, `Consistency Rules`, `Retention And Data Lifecycle`. |
| Add scheduling/backpressure/cost controls | Complete | `Scheduling, Quotas, And Cost Controls`, `Scheduler Queues`, `Backpressure And Fairness`, `Cost Controls`. |
| Add repo cache and provider limits | Complete | `Source Checkout Strategy`, repo cache architecture and requirements. |
| Add webhook-first PR monitor | Complete | `PR Monitor And Merge Queue At Fleet Scale`. |
| Add BYOK and egress design | Complete | `BYOK Key Isolation`, `Egress Enforcement Architecture`. |
| Add observability cardinality and cell health | Complete | `Cardinality Rules`, `Cell Health Model`, SLO updates. |
| Add failure modes, tests, rollout, risks | Complete | `Failure Modes Registry`, `Coverage Diagram`, `Delivery Phases`, `Risks And Mitigations`. |

## Source Consistency Check

- GKE quotas and large-cluster docs support the decision not to design around a
  single maximum-size cluster. The PRD uses cells and budgets instead.
- GKE batch workload docs support using Kubernetes Jobs/Kueue-style admission
  inside selected cells while AWF keeps product-level scheduling.
- GKE fleet docs support using fleets as operational grouping, while the PRD
  keeps tenant authorization in AWF rather than fleet sameness.
- Cloud SQL docs support avoiding "one giant Postgres" as the end-state control
  store.
- Spanner, Bigtable, and Cloud Storage docs support splitting hot state, event
  history, and artifact bytes.

## Verification Commands

```bash
rg -n "^(#|##|###) " docs/AWF_ON_GKE_PRD.md
rg -n "Industrial Scale|cell|Spanner|Bigtable|Kueue|PR monitor|outbox|object budget|cardinality|Failure Modes|Build And Distribution|500,000|100,000" docs/AWF_ON_GKE_PRD.md
LC_ALL=C rg -n "[^\\x00-\\x7F]" docs/AWF_ON_GKE_PRD.md || true
wc -l docs/AWF_ON_GKE_PRD.md
```

Results:

- Required industrial-scale sections and keywords are present.
- No non-ASCII characters were found in `docs/AWF_ON_GKE_PRD.md`.
- `docs/AWF_ON_GKE_PRD.md` is now 1,993 lines.

## Remaining Open Decisions

The PRD intentionally leaves several engineering choices open until load tests
and customer isolation requirements are known:

- GKE Standard versus Autopilot for early beta.
- Direct Kubernetes API versus private `WorkspaceRun` CRD.
- Spanner from first beta versus sharded Postgres with a Spanner migration gate.
- Kueue scope: validation Jobs only versus all workspace Jobs inside cells.
- Initial cell active-workspace budget.
- Which customers require dedicated cells before BYOK beta.
