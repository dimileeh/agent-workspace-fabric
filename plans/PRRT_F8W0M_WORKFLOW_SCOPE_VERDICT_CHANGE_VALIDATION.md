# PRRT F8W0M Workflow Scope Verdict Change Validation

Plan reference: `plans/PRRT_F8W0M_WORKFLOW_SCOPE_VERDICT_CHANGE_PLAN.md`

## Requirement Status

- Add a regression test that reproduces `fix_committed` followed by `false_positive` for the same inline thread in one fix cycle: Complete.
- Ensure the latest verdict controls workflow-scope push-failure rollback/requeue behavior: Complete.
- Preserve existing workflow-scope handling for current `fix_committed` items: Complete.
- Run only focused validation for the changed area; full AWF/GitHub validation remains owned by AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`

Focused commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_honors_latest_false_positive_thread_verdict -q`
  - First run before implementation failed with `T_multi` marked `needs_human`.
  - Re-run after implementation passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -k 'workflow_scope_push_failure or workflow_scope_requeue' -q`
  - Passed: 8 passed, 23 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Passed.

Full AWF/GitHub validation was not run inside the agent phase per the workspace contract; AWF owns broad validation after completion.
