# PRRT_kwDOSJAM6s6KxJt9 Validation

Plan reference: `PRRT_kwDOSJAM6s6KxJt9_PLAN.md`

## Requirement Status

- Verify the current implementation against the review thread: Complete.
- Treat recovered diff collection failure as a reason-coded unrecoverable HEAD-object error: Complete.
- Preserve the existing supply-chain policy refresh behavior for successful recovered diffs: Complete.
- Add or update a focused regression test for the changed failure behavior: Complete.
- Run only targeted validation for the touched test: Complete.

## Evidence

- `src/awf/runtime/pr_monitor_runner/remote_repair.py` now raises `_MonitorHeadObjectMissingError` with `HEAD_OBJECT_MISSING_UNRECOVERABLE` when the recovered diff command fails.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py` updates the existing recovered-diff regression to assert the reason-coded exception and no supply-chain policy call.
- Initial test-first run failed before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -k recovered_diff_fails -q`
- Passing focused validation after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -k recovered_diff_fails -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -k missing_head_recovery -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Full AWF/GitHub validation is managed by AWF after agent completion per the workspace contract.
