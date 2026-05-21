# PRRT_kwDOSJAM6s6DyuL2 Requested Queue Signature Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DyuL2` reports that the requested-capacity
resume cursor invalidation signature samples the requested queue by
`Workspace.id`, while `_claim_requested_ids_with_capacity` resumes in scheduler
order. When the requested queue is larger than the signature limit, priority
changes or new urgent work outside the lexical ID sample can leave the resume
cursor active and skip scheduler-head work.

Scope is limited to requested-capacity resume signature sampling and regression
coverage in the control worker tests.

## Requirements Checklist

- Add a regression test showing that a workspace outside the first 500 IDs but
  inside the scheduler frontier changes the requested queue signature.
- Preserve bounded signature scans.
- Keep the signature based on requested work schedulable for the worker node.
- Preserve existing signature sensitivity to queue composition and scheduler
  policy/profile fields.
- Do not change branch, push, or weaken existing safety tests.

## Implementation Steps

1. Add the regression test first in `tests/unit/control/test_worker.py`.
2. Confirm the new regression fails against the current ID-ordered sampling.
3. Update `_requested_capacity_queue_signature` in `src/awf/control/worker.py`
   so the bounded frontier is ordered by scheduler priority, effective score,
   queue time, and workspace ID.
4. Run the targeted regression and nearby signature tests.
5. Run the narrow worker test selection that proves resume invalidation remains
   correct.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature_changes_when_scheduler_frontier_changes_beyond_id_sample"`
  - Passes after implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature or requested_capacity_gate_resets_resume_cursor_when_requested_queue_changes"`
  - Passes with no regressions in queue signature or requested queue cursor reset behavior.
