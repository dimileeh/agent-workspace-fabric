# Review PRRT_kwDOSJAM6s6Fggfy Null Provisioning Slots Plan

## Problem Statement

The worker admission row-slot guard counts active workspaces for the configured
worker node. A newly claimed provisioning workspace can still have
`Workspace.node_id` set to `NULL` until the provisioner stamps placement, so a
restart or second worker with the same configured `node_id` can miss that
occupied slot and admit another requested workspace.

## Scope

- Keep the existing behavior where a worker with `node_id=None` only counts
  unassigned active rows and ignores named-node rows.
- For a worker with a configured `node_id`, count active rows assigned to that
  node and active rows whose `node_id` is still `NULL`.
- Add a focused regression test for the review-thread scenario.
- Avoid broad AWF/GitHub-owned validation; run only targeted checks for the
  touched worker admission behavior.

## Requirements Checklist

- [ ] Regression test demonstrates a configured-node worker treats a `NULL`
  `node_id` provisioning row as occupying the last execution slot.
- [ ] `_requested_admission_row_slots()` preserves null-node worker isolation
  from named-node active rows.
- [ ] Focused validation passes after the fix.
- [ ] Validation notes explicitly leave broad validation to AWF/GitHub after
  agent completion.

## Implementation Steps

1. Add a failing regression to
   `tests/unit/control/test_worker_scheduler_admission.py`.
2. Run the new targeted test to confirm the existing bug.
3. Update `src/awf/control/worker/manager.py` to include `NULL` placement rows
   when `WorkerConfig.node_id` is configured.
4. Re-run focused tests covering both the new scenario and the existing
   null-node isolation scenario.
5. Record validation in the matching `_VALIDATION.md` file.
