# Defaulted Ordered Queue Decision Plan

## Problem Statement and Scope

PR review comment `issue:4495131102` reports that requested workspaces claimed under the local capacity scheduler with defaulted reservation demand record two `ordered` queue decisions: one inside the advisory-lock transaction with reason `LOCAL_CAPACITY_RESERVATION_DEFAULTED`, then another after the transaction with reason `ORDERED_REQUESTED_PROVISIONING`.

Scope is limited to preserving a single ordered admission record for this defaulted-demand provisioning path without changing scheduling order, capacity accounting, ready execution decisions, monitor resume decisions, or non-defaulted requested provisioning.

## Requirements Checklist

- Add a regression test proving a defaulted-demand requested workspace claimed by the capacity gate has exactly one `ordered` queue decision.
- Preserve the defaulted-reservation reason record so analytics can still see that defaulted demand was used.
- Keep ordinary requested provisioning, ready execution, and monitor resume ordered-decision behavior unchanged.
- Preserve retry dedupe behavior for ambiguous ordered-decision commits.
- Run the narrow affected tests and document validation evidence.

## Implementation Steps

1. Add a unit regression around the local capacity requested path with configured capacity and no active reservation.
2. Confirm the new test fails on the current implementation with two ordered decisions.
3. Update ordered-decision dedupe logic so `ORDERED_REQUESTED_PROVISIONING` treats the latest `LOCAL_CAPACITY_RESERVATION_DEFAULTED` ordered decision for the same workspace/task/attempt as already recorded.
4. Re-run the regression and nearby ordered-decision tests.
5. Create `plans/defaulted_ordered_queue_decision_VALIDATION.md` with requirement status and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "defaulted_ordered or ordered_decision"`
  - Passes with the new regression and existing ordered-decision tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes with no lint issues.
