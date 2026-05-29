# Provisioning Recovery Lease Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FkEXj` reports that named workers stamp
`node_id` when claiming `requested -> provisioning`, making a live provisioning
row visible to stale recovery before the provisioner persists compose metadata.
Another worker can then classify the row as stranded and fail it.

Scope is limited to worker provisioning claims and stale-active recovery guards.

## Requirements

- Add regression coverage for a live, freshly claimed named-node provisioning row
  with no compose metadata.
- Preserve recovery of truly stale provisioning rows that occupy admission slots.
- Apply the same protection to the local-capacity claim path and direct requested
  claim path.
- Keep validation focused; AWF/GitHub owns broad validation after the agent exits.

## Implementation Steps

1. Add a focused unit regression in `tests/unit/control/test_worker_scheduler_admission.py`.
2. Stamp a worker-owned execution lease when claiming a workspace for
   provisioning.
3. Refresh that lease while `provision_claimed()` runs and release it afterward.
4. Make stale-active recovery list and fail/recover rechecks require a stale
   execution lease before acting on `provisioning`.
5. Run targeted tests for worker admission/stale recovery behavior.

## Verification

- First run the new targeted regression before implementation and confirm it
  fails.
- After implementation, run the targeted regression and nearby worker admission
  tests touched by this behavior.
- Do not run full coverage, whole-repository pytest, or CI-equivalent validation
  in this agent phase.
