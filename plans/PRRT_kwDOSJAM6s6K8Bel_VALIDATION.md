# PRRT_kwDOSJAM6s6K8Bel Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K8Bel_PLAN.md`

## Requirement Status

- Add a regression test proving a primary `rev-parse HEAD` SHA is not accepted
  when the canonical mirror lacks that commit object: Complete.
- Update `_repair_operation_start_head_result` so the primary `rev-parse HEAD`
  path verifies the commit exists before returning it: Complete.
- Preserve existing fallback behavior for unavailable or invalid primary heads:
  Complete.
- Run only focused validation for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion: Complete.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/remote_repair.py` to run the
  existing mirror `cat-file -e <sha>^{commit}` guard before accepting a primary
  start-head SHA.
- Added
  `test_repair_operation_start_head_rejects_dangling_primary_head` in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k test_repair_operation_start_head_rejects_dangling_primary_head`
- Confirmed focused repair start-head coverage passes after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k repair_operation_start_head`
- Confirmed focused lint passes:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`

Full AWF/GitHub validation is intentionally left to the post-agent AWF-managed
validation flow per the workspace contract.
