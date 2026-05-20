# PRRT_kwDOSJAM6s6Da11 Capacity Scan Resume Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Da11-` reports that the local-capacity
requested claim loop can permanently hide claimable requested work behind a
long prefix of capacity-blocked workspaces. The current loop intentionally
bounds each poll's full-page scan, but every poll restarts at the queue head.

Scope is limited to requested workspace provisioning with local capacity
gating in `src/awf/control/worker.py`, focused regression coverage in
`tests/unit/control/test_worker.py`, and this plan/validation record.

## Requirements Checklist

- Preserve the existing per-poll bound for fully blocked requested queues while
  holding the local capacity scheduler lock.
- Avoid permanent starvation by resuming the next poll after the last scanned
  blocked page when local allocated capacity has not changed.
- Reset the resume cursor when allocated capacity changes, the requested queue
  is exhausted, or provisioning slots are filled.
- Keep existing scheduler ordering within each scanned page, queue-decision
  recording, and max-concurrent provisioning semantics.
- Add a regression test where a fitting requested workspace beyond the bounded
  scan window is claimed on a later poll.

## Implementation Steps

1. Add a failing unit test proving that a fitting requested workspace behind
   more blocked pages than the per-poll scan window is eventually claimed by
   the same worker across polls.
2. Update the capacity claim operation to return both claimed IDs and the next
   scan resume cursor after commit.
3. Track the resume cursor with an allocated-capacity signature so the worker
   only resumes past blocked pages when capacity state is unchanged.
4. Verify the focused regression and the existing requested-capacity tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_resumes_after_bounded_blocked_scan -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate"`
  passes after implementation.
