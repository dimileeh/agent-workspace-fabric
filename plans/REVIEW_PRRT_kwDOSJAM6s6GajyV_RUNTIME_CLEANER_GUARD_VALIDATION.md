# Review PRRT_kwDOSJAM6s6GajyV Runtime Cleaner Guard Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6GajyV_RUNTIME_CLEANER_GUARD_PLAN.md`

## Requirement Status

- Confirm the review feedback is actionable against current code: Complete.
  - The new regression failed before the code change because `_release_terminal_runtime_resources` called `_resume_pending_planning_scope_auto_retries_after_terminal_release(limit=5)` with `_runtime_cleaner=None`.
- Add a regression test proving a worker without a runtime cleaner does not scan or resume planning-scope auto-retries: Complete.
  - Added `test_release_terminal_runtime_resources_skips_without_runtime_cleaner`.
- Restore the runtime-cleaner guard so terminal runtime release work, including dependent planning retry resume scans, is skipped when no cleaner is configured: Complete.
  - `src/awf/control/worker/cleanup.py` now returns early when `self._runtime_cleaner is None`.
- Run focused tests only; full AWF/GitHub validation remains owned by AWF after agent completion: Complete.
  - Full AWF/GitHub validation was not run locally per workspace contract.
- Record validation evidence in a matching validation document: Complete.

## Evidence

Changed files:

- `src/awf/control/worker/cleanup.py`
- `tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py`

Focused commands:

- Failed before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py -q -k "skips_without_runtime_cleaner"`
  - Failure: `retry resume should not run for limit 5`
- Passed after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py -q -k "release_terminal_runtime_resources"`
  - Result: `4 passed, 41 deselected`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_041.py tests/unit/control/test_worker_parts/test_worker_part_042.py -q -k "release_does_not_run_when_runtime_cleaner_not_configured or release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release or default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node"`
  - Result: `3 passed, 21 deselected`
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py`
  - Result: `All checks passed!`

## Gaps

None.
