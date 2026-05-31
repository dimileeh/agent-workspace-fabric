# PRRT_kwDOSJAM6s6F658f Workflow Scope Retry Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F658f_WORKFLOW_SCOPE_RETRY_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add a regression proving workflow-scope comment-repair push failures requeue publish-dependent `fix_committed` review items. | Complete | Updated `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py` and `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`. Initial focused run failed because state still held `needs_human`. |
| Prove the requeued state makes `decide()` return `AddressComments`, not `NotifyHuman`, for the same unresolved thread. | Complete | `test_workflow_scope_push_failure_requeues_fix_committed_thread_state` asserts the post-failure decision is `AddressComments` with the workflow thread. |
| Preserve non-fix verdicts such as `false_positive` and captured `defer`. | Complete | `test_workflow_scope_requeue_preserves_non_fix_verdicts` and the mixed-thread fix-cycle test assert those verdicts remain addressed. |
| Keep non-workflow push-failure rollback behavior unchanged. | Complete | Production change is limited to the `GITHUB_WORKFLOW_SCOPE_REQUIRED` branch; `test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues` still passes. |
| Run only focused local checks. | Complete | Ran only targeted workflow-scope tests plus lint/type checks for touched files. Full AWF/GitHub validation is managed after agent completion. |

## Verification Evidence

- Initial failing check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope`
    - Failed during collection before the new helper existed.
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`
    - Failed because `T_workflow_scope` remained `needs_human`.
- Passing checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope`
    - `16 passed, 3 deselected`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`
    - `1 passed`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues -q`
    - `1 passed`
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
    - Passed
  - `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/fix_cycle.py`
    - Passed
