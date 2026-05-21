# Capacity Scheduler Review 4495131102 Plan

## Problem Statement and Scope

Address PR review comment `issue:4495131102` for the local-node capacity
scheduler. The reviewer identified two worker-level issues:

- capacity-aware requested claims can scan an unbounded number of full requested
  pages while holding the local-node advisory lock when no candidates fit;
- the capacity path pre-fetches requested workspace IDs before taking the lock,
  but the locked capacity scan re-fetches the schedulable workspace rows and
  does not use those IDs.

Scope is limited to `ControlWorker` capacity scheduling behavior and focused
unit tests. No GitHub comments, pushes, branch changes, or unrelated refactors.

## Requirements Checklist

- Add a regression test proving the capacity path bounds full-page scans when
  every scanned candidate is capacity-blocked.
- Add a regression test proving capacity-aware provisioning does not call the
  pre-lock requested-ID listing path.
- Keep the existing ability to scan past the first blocked candidate window far
  enough to find a fitting candidate in the next refill page.
- Preserve local capacity advisory locking, queue-decision recording, and
  max-concurrent provisioning semantics.
- Remove the unused `workspace_ids` dependency from the capacity claim helper.
- Update any stale tests whose assertions specifically depended on the removed
  pre-lock capacity prefetch.

## Implementation Steps

1. Add failing tests in `tests/unit/control/test_worker.py` for the bounded
   scan and no-prefetch capacity path.
2. Run the focused new tests and confirm they fail against the current code.
3. Update `src/awf/control/worker.py` so capacity-aware `run_once` enters the
   locked capacity claim directly, with no pre-lock requested-ID list/filter.
4. Bound the capacity paging loop after a full page produces no claims, using
   the existing scheduler refill page constant.
5. Adjust stale tests only where they assert the removed pre-lock capacity
   behavior.
6. Run the focused worker tests, then the narrow unit file if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_bounds_fully_blocked_page_scan or capacity_gate_claims_without_prefetching_requested_ids"`
  - Passes after implementation; fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate or concurrent_capacity_claims or capacity_requested_race or dispatches_requested"`
  - Passes with existing capacity scheduler behavior preserved.
- If time permits, run:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
