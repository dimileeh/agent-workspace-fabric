# Review Issue 4585067239 Workflow Scope Needs Human Plan

## Problem Statement and Scope

Review feedback reports that comment-repair workflow-scope push failures clear
affected review items back to unaddressed state. That can make the PR monitor
retry the same repair on later iterations even though GitHub rejected the push
because the token lacks `workflow` scope, which requires operator action.

Scope is limited to PR monitor comment-repair state handling after
`GITHUB_WORKFLOW_SCOPE_REQUIRED` push failures and focused regressions for the
affected fix-cycle and outer-loop behavior.

## Requirements Checklist

- Mark publish-dependent `fix_committed` inline threads and review comments as
  `needs_human` with the exact workflow-scope permission reason after a
  workflow-scope push rejection.
- Preserve body-hash state so stale-state cleanup does not immediately clear
  the stored `needs_human` verdict.
- Preserve push-independent inline `defer` and `false_positive` verdicts during
  workflow-scope push rejection handling.
- Ensure subsequent monitor decisions do not re-enter comment repair for the
  workflow-scope-blocked items and can surface the stored needs-human reason.
- Run only focused local validation for the changed monitor behavior; leave
  broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Update focused regression tests to expect workflow-scope-blocked
   `fix_committed` items to become `needs_human`, while `defer` and
   `false_positive` items remain addressed.
2. Run the focused tests and confirm they fail against the current requeue
   behavior.
3. Replace workflow-scope requeue cleanup with a helper that marks only
   publish-dependent fix verdicts as `needs_human` using the push error message.
4. Keep generic push-failure cleanup unchanged so non-workflow push failures
   still clear publish-dependent state.
5. Re-run the focused tests and lint for the changed files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k 'workflow_scope_push_failure or workflow_scope_marks or notify_human_reason'`
  - Fails before implementation, then passes after the behavior change.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q -k 'workflow_scope'`
  - Fails before implementation, then passes after the outer-loop expectations
    are updated and implemented.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  - Passes after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
