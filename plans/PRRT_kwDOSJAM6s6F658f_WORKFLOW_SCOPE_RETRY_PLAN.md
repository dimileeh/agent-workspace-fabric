# PRRT_kwDOSJAM6s6F658f Workflow Scope Retry Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F658f` reports that comment-repair fixes committed
locally become stuck when GitHub rejects the push because the token lacks
`workflow` scope. The current workflow-scope branch converts affected
`fix_committed` review items to `needs_human`; after an operator grants a token
with workflow permission, the monitor still treats the unresolved items as
already triaged and returns `NotifyHuman` instead of re-entering comment repair
and retrying the push.

Scope is limited to PR monitor comment-repair state handling for
`GITHUB_WORKFLOW_SCOPE_REQUIRED`. Sync-base and CI-repair terminal behavior are
out of scope.

## Requirements Checklist

- Add a regression proving a workflow-scope comment-repair push failure requeues
  publish-dependent `fix_committed` review items for a later monitor iteration.
- Prove the requeued state makes `decide()` return `AddressComments`, not
  `NotifyHuman`, when the same unresolved thread is still present.
- Preserve already handled non-publish-fix verdicts such as `false_positive` and
  captured `defer`.
- Keep non-workflow push-failure rollback behavior unchanged.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Update focused PR-monitor regressions to expect workflow-scope failures to
   clear `fix_committed` addressed state instead of marking it `needs_human`.
2. Add direct `decide()` coverage showing the same unresolved thread routes back
   to `AddressComments` after the workflow-scope failure.
3. Replace the workflow-scope `needs_human` marker helper with a helper that
   clears only currently `fix_committed` publish-dependent items.
4. Run the narrow tests covering the changed fix-cycle behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope`
  - Fails before implementation for the new retry expectation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`
  - Passes after updating the `_execute(AddressComments)` regression.

Full AWF/GitHub validation is intentionally not run during this agent phase.
