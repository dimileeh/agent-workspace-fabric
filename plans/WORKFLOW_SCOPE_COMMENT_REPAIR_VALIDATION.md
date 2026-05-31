# Workflow Scope Comment Repair Validation

Plan reference: `WORKFLOW_SCOPE_COMMENT_REPAIR_PLAN.md`

## Requirement Status

- Regression proving comment repair does not terminate on
  `GITHUB_WORKFLOW_SCOPE_REQUIRED`: Complete.
  - Added
    `test_monitor_comment_repair_workflow_scope_failure_marks_needs_human_without_terminating`.
  - Confirmed it failed before the implementation with `assert True is False`
    for the terminal result.
- Preserve `needs_human` state and stored reason for the affected thread:
  Complete.
  - The regression asserts the thread is marked `needs_human` and the stored
    reason names `.github/workflows/publish.yml` and missing `workflow` scope.
- Preserve terminal behavior for sync-base and CI repair workflow-scope failures:
  Complete.
  - Sync-base and CI repair now opt into workflow-scope termination at their
    call sites.
  - Existing focused sync-base and CI repair tests pass.
- Keep protected-scope and repair-start terminal failures unchanged: Complete.
  - The shared `terminal_monitor_failure` property still includes protected
    scope and repair-start reason codes.
  - The existing generic comment push failure regression still passes.
- Run only focused local checks: Complete.
  - Full AWF/GitHub validation was not run; AWF owns broad validation after
    agent completion.

## Files Changed

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `src/awf/runtime/pr_monitor_runner/loop.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `plans/WORKFLOW_SCOPE_COMMENT_REPAIR_PLAN.md`

## Verification Evidence

- Failing-before check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_marks_needs_human_without_terminating -q`
  - Failed before implementation because `_execute(AddressComments)` returned
    `terminal is True`.
- Passing focused behavior checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_marks_needs_human_without_terminating -q`
  - Result: `1 passed`.
- Passing adjacent regression checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_marks_needs_human_without_terminating tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_git_push_result_maps_github_workflow_scope_rejection tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py::test_execute_sync_base_workflow_scope_push_failure_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_execute_ci_fix_workflow_scope_push_failure_is_terminal -q`
  - Result: `5 passed`.
- Focused lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Result: `All checks passed!`

## Gaps

No planned requirements remain missing or partial. Broad validation and merge
gating are left to AWF/GitHub per workspace contract.
