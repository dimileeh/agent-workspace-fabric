# Protected Scope Repair Head Verification Validation

Plan reference: `plans/PROTECTED_SCOPE_REPAIR_HEAD_VERIFY_PLAN.md`

## Requirement Status

- Add a regression test that fails before implementation by proving post-agent protected-scope repair verifies `HEAD` before provider-error handling: Complete.
  - Evidence: `test_protected_scope_repair_verifies_head_before_provider_retry` failed before the implementation with `DID NOT RAISE _MonitorHeadObjectMissingError`, then passed after the implementation.
- Add a regression test or assertion that status-based decisions are also gated by the same `HEAD` verification: Complete.
  - Evidence: `test_protected_scope_repair_verifies_head_before_status_recheck` failed before the implementation with `DID NOT RAISE _MonitorHeadObjectMissingError`, then passed after the implementation.
- Reuse the existing missing-HEAD error path; do not introduce broad new recovery behavior: Complete.
  - Evidence: `remote_repair_protected.py` now calls `verify_head_object_exists()` after post-agent mirror cleanup and raises `_MonitorHeadObjectMissingError` with `_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON` before provider/status handling.
- Keep changes minimal and avoid protected workflow/config changes: Complete.
  - Evidence: changes are limited to `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`, one focused unit test file, and plan/validation docs.
- Run targeted tests only; AWF/GitHub own broad validation after agent completion: Complete.
  - Evidence: only focused pytest selections and file-scoped ruff were run locally.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_verifies_head_before_provider_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_verifies_head_before_status_recheck -q`
  - Before implementation: failed as expected.
  - After implementation: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py -k protected_scope_repair -q`
  - Result: `13 passed, 18 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  - Result: `All checks passed`.

## Notes

The broader nearby command without `-k protected_scope_repair` was attempted and produced two failures in `_repair_operation_start_head_result` fallback tests:

- `test_repair_operation_start_head_accepts_mocked_no_mirror_fallback`
- `test_repair_operation_start_head_rejects_no_mirror_fallback_when_guard_fails`

Those failures are outside this thread's protected-scope repair path and did not involve the changed module. Full AWF/GitHub validation remains managed by AWF after agent completion.
