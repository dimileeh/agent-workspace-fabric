# Workflow Scope Comment Repair Notification Validation

Plan reference:
`plans/WORKFLOW_SCOPE_COMMENT_REPAIR_NOTIFICATION_PLAN.md`

## Requirement Status

- Preserve `GITHUB_WORKFLOW_SCOPE_REQUIRED` as non-terminal for comment repair:
  Complete. The AddressComments path still returns `False` and increments
  monitor iteration state after the failed push.
- Preserve requeue behavior for publish-dependent items:
  Complete. `_requeue_workflow_scope_publish_dependent_items` is unchanged, and
  existing requeue assertions still pass.
- Notify the operator with the exact workflow-scope blocker reason:
  Complete. The AddressComments push-failure branch now calls
  `_post_human_notification_once` with the parsed push error message when
  `push_result.workflow_scope_required` is true.
- Avoid notification spam:
  Complete. The fix uses the existing one-shot notification helper, which keys
  notifications by head SHA and blocker reason.
- Keep validation focused:
  Complete. Only targeted unit and lint checks were run; full AWF/GitHub
  validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/loop.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passed: 22 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q`
  - Passed: 20 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  - Passed.

## Remaining Gaps

None for the planned scope. Full repository validation, coverage gates, and
CI-equivalent checks were intentionally not run in this agent phase.
