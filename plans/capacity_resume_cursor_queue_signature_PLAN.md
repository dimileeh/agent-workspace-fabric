# Capacity Resume Cursor Queue Signature Plan

## Problem Statement And Scope

The local-capacity scheduler stores a resume cursor after a bounded scan of fully
blocked requested workspaces. The cursor is currently reused when allocated
capacity is unchanged, but it does not detect changes to the requested queue
itself. A newly requested workspace that sorts before the stored cursor can be
skipped for later polls even when it fits available capacity.

Scope is limited to capacity-gated requested workspace claiming in
`src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test proving a new fitting high-priority requested workspace
  inserted ahead of a stored capacity resume cursor is claimed on the next poll.
- Keep the bounded blocked-page resume behavior when the requested queue is
  unchanged.
- Reset the capacity resume cursor when the requested queue for the worker's
  local node scope changes.
- Preserve existing allocated-capacity signature invalidation.
- Avoid weakening existing scheduler priority, capacity, and bounded-scan tests.

## Implementation Steps

1. Add the failing regression near the existing capacity bounded-scan tests.
2. Add a requested-queue signature for capacity resume state.
3. Compare the stored queue signature with the current requested queue signature
   before reusing `resume_after`.
4. Store the queue signature only when returning a new resume cursor.
5. Run the targeted worker test(s), then the narrow unit surface justified by
   the change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_resets_resume_cursor_when_requested_queue_changes or requested_capacity_gate_resumes_after_bounded_blocked_scan"`
  must pass.
- If practical, run `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  to verify the broader worker unit surface.
