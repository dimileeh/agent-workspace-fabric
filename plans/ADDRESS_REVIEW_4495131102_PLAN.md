# Address Review Comment 4495131102 Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` reports two capacity-scheduling concerns:

1. `_claim_requested_ids_with_capacity` can emit misleading `worker.skip_stale_dispatch`
   logs for the pre-lock `workspace_ids` when concurrent workers legitimately claimed
   those rows before this worker acquired the local-capacity advisory lock.
2. Saturation metrics may report `allocated_resources` and `allocated_capacity`
   cluster-wide instead of scoped to the scheduler node.

Scope is limited to the review comment. Do not change branch, push, or broaden the
scheduler/metrics behavior beyond the reported issues.

## Requirements Checklist

- Add a regression test for the capacity-claim race where the pre-lock requested
  IDs are already claimed before the capacity-locked scan runs.
- Ensure that race does not emit `worker.skip_stale_dispatch` for IDs the
  capacity path did not actually try to transition.
- Preserve real stale logging when `transition_if_current` loses a race for a
  concrete capacity candidate.
- Verify whether metrics allocation summaries are already node-scoped; change
  only if the local code still shows the reported bug.
- Run focused tests for touched behavior.

## Implementation Steps

1. Add a worker unit test that reproduces the misleading log by simulating a
   concurrent claim between `_filter_current_status` and the capacity-locked
   claim path.
2. Confirm the new test fails against the current code.
3. Remove the no-candidates stale-log path from `_claim_requested_ids_with_capacity`;
   keep stale logging inside `_claim_requested_capacity_candidates` for actual
   lost transition attempts.
4. Re-run the new worker test and relevant metrics test(s).
5. Record plan validation in `plans/ADDRESS_REVIEW_4495131102_VALIDATION.md`.
