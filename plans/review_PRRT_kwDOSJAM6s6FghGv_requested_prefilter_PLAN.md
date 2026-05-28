# Review PRRT_kwDOSJAM6s6FghGv Requested Prefilter Plan

## Problem Statement and Scope

The non-local-capacity provisioning path in `ControlWorker.run_once` truncates
listed requested workspace IDs to the available provision slots before checking
that those rows are still in `requested` status. If a stale candidate appears
near the front of the list, a run cycle can dispatch fewer workspaces than the
available slots even when later listed candidates are still eligible.

Scope is limited to the non-local-capacity requested provisioning path and its
focused regression coverage.

## Requirements Checklist

- Add a regression test showing that stale requested candidates are filtered
  before applying the current cycle's provision-slot limit.
- Preserve local-capacity behavior, which already claims through the capacity
  gate without pre-listing requested IDs.
- Keep ordered decision recording and provisioning dispatch limited to the final
  claimed IDs.
- Run only focused validation for the changed worker behavior; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add a synthetic `run_once` test that returns more listed requested IDs than
   the current slot count, marks the first listed ID stale in
   `_filter_current_status`, and expects the worker to claim and dispatch later
   eligible IDs up to the slot count.
2. Run the focused new test and confirm it fails against the current
   pre-filter truncation behavior.
3. Update `ControlWorker.run_once` so the non-local-capacity branch filters the
   listed IDs first, then slices to `requested_provision_slots`, then claims.
4. Re-run the focused test and any narrow adjacent worker test needed to prove
   the behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_001.py -q -k stale_requested_candidates`
  - Passes after the implementation and fails before it.
- Full AWF/GitHub validation is intentionally not run in this agent phase.
