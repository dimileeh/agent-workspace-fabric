# Comment 3320494481 Atomic Admission Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Fg1jo` reports that requested-workspace
admission computes execution-row availability before the later
`requested -> provisioning` claim. Two control workers on the same node can
both observe one free slot and then claim different requested rows, exceeding
`max_concurrent_executions`.

Scope is limited to requested provisioning admission in the control worker and
focused regressions for the concurrent claim race. Broad AWF/GitHub validation
remains owned by AWF after this agent phase.

## Requirements Checklist

- Add a regression that forces two workers to observe the same stale admission
  slot and proves only one requested row can enter `provisioning`.
- Cover both ordinary requested claims and local-capacity requested claims, since
  both use the precomputed admission slot count before claiming.
- Make the slot reservation atomic by serializing requested admission per worker
  node and rechecking active admission rows in the claim transaction.
- Preserve existing stale-requested logging and local-capacity queue behavior.
- Run only focused tests for the touched worker admission behavior.

## Implementation Steps

1. Add deterministic concurrency tests in
   `tests/unit/control/test_worker_scheduler_admission.py`.
2. Introduce shared requested-admission helpers for transaction-scoped active-row
   slot counting and a PostgreSQL transaction advisory lock.
3. Use the helpers in ordinary `_claim_requested_for_provisioning` claims and
   local-capacity `_claim_requested_ids` claims so stale prechecks cannot claim
   past row capacity.
4. Reuse the transaction-scoped slot counter from
   `ControlWorker._requested_admission_row_slots` to keep filtering behavior
   consistent.
5. Run the focused worker admission tests and record evidence in validation.

## Verification Commands

- Expected red first:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
- Expected green after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`

Pass criteria: the focused admission test module passes, and validation notes
that full AWF/GitHub validation is intentionally left to AWF after agent
completion per workspace contract.
