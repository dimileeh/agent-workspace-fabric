# Review PRRT_kwDOSJAM6s6FghGv Requested Prefilter Validation

Plan reference: `plans/review_PRRT_kwDOSJAM6s6FghGv_requested_prefilter_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing that stale requested candidates are
  filtered before applying the current cycle's provision-slot limit.
- Complete: Preserved local-capacity behavior; the local-capacity branch still
  claims with `limit=requested_provision_slots` and does not pre-list requested
  IDs.
- Complete: Ordered decision recording and provisioning dispatch still operate
  only on final claimed IDs.
- Complete: Ran focused validation only. Full AWF/GitHub validation is managed
  after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `tests/unit/control/test_worker_parts/test_worker_part_001.py`
- `plans/review_PRRT_kwDOSJAM6s6FghGv_requested_prefilter_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6FghGv_requested_prefilter_VALIDATION.md`

Commands run:

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_001.py -q -k stale_requested_candidates`
  - Result: failed because only `ws-fresh-a` was claimed after pre-filter
    truncation.
- Passing focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_001.py -q -k stale_requested_candidates`
  - Result: passed.
- Passing adjacent worker-part tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_001.py -q`
  - Result: 10 passed.
- Passing targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_parts/test_worker_part_001.py`
  - Result: all checks passed.

## Remaining Gaps

None for the planned scope.
