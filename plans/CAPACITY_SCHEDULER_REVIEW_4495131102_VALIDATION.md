# Capacity Scheduler Review 4495131102 Validation

Plan reference: `plans/CAPACITY_SCHEDULER_REVIEW_4495131102_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving the capacity path bounds full-page
  scans when every scanned candidate is capacity-blocked.
  - Evidence: `test_requested_capacity_gate_bounds_fully_blocked_page_scan`
    verifies only the first page plus the configured refill page are scanned
    and that unscanned tail workspaces receive no queue decisions.
- Complete: Add a regression test proving capacity-aware provisioning does not
  call the pre-lock requested-ID listing path.
  - Evidence: `test_requested_capacity_gate_claims_without_prefetching_requested_ids`
    fails if `_list_requested` is invoked during a capacity claim.
- Complete: Keep the existing ability to scan past the first blocked candidate
  window far enough to find a fitting candidate in the next refill page.
  - Evidence: existing
    `test_requested_capacity_gate_scans_past_blocked_candidate_window` passes.
- Complete: Preserve local capacity advisory locking, queue-decision recording,
  and max-concurrent provisioning semantics.
  - Evidence: existing requested-capacity, concurrent-capacity, and requested
    dispatch worker tests pass.
- Complete: Remove the unused `workspace_ids` dependency from the capacity claim
  helper.
  - Evidence: `_claim_requested_ids_with_capacity` no longer accepts or sizes
    batches from pre-fetched workspace IDs.
- Complete: Update stale tests whose assertions specifically depended on the
  removed pre-lock capacity prefetch.
  - Evidence:
    `test_capacity_requested_path_skips_prelock_status_filter` now asserts the
    capacity path does not run the pre-lock status filter.

## Commands Run

- Expected failing pre-implementation check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_claims_without_prefetching_requested_ids or capacity_gate_bounds_fully_blocked_page_scan"`
  - Result before implementation: failed for the dead prefetch and unbounded
    scan behaviors.
- Focused regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_claims_without_prefetching_requested_ids or capacity_gate_bounds_fully_blocked_page_scan"`
  - Result after implementation: passed, `2 passed`.
- Capacity behavior slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate or concurrent_capacity_claims or capacity_requested_race or capacity_requested_path or dispatches_requested"`
  - Result: passed, `15 passed`.
- Full worker unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Result: passed, `200 passed`.
- Static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- Type checks:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- Full ruff pass:
  `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed.
- Whitespace sanity:
  `git diff --check`
  - Result: passed.

## Remaining Gaps

None.
