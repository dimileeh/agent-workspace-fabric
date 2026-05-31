# COMMENT_3330063753 Review Comment Workflow-Scope Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F7peI` reports that a workflow-scope push
failure can leave a publish-dependent top-level review-comment verdict marked
addressed even though the corresponding local fix was not pushed. The next
monitor poll can then skip the unresolved review comment and allow merge logic
to evaluate an un-repaired remote PR head.

Scope is limited to PR monitor fix-cycle state cleanup after
`GITHUB_WORKFLOW_SCOPE_REQUIRED` push failures and focused regression coverage.

## Requirements Checklist

- Clear publish-dependent review-comment verdict state after workflow-scope push
  failures when the required content has not been pushed.
- Preserve non-publish-dependent `needs_human` / `agent_failed` review-comment
  verdicts across push failures.
- Preserve deferred-issue idempotency markers for inline-thread defer capture.
- Keep existing inline-thread workflow-scope cleanup behavior intact.
- Add focused regression coverage for review-comment requeue behavior.
- Run only targeted checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a failing unit regression showing a workflow-scope push rejection clears a
   review-comment `false_positive`/body-hash marker so `decide()` requeues the
   unresolved review comment.
2. Update `_requeue_workflow_scope_publish_dependent_items` so all
   publish-dependent IDs are cleared on workflow-scope failure, while preserving
   the existing special marker survival behavior.
3. Confirm existing inline-thread workflow-scope tests still pass.
4. Add this validation document with requirement status and focused evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes with the new regression and existing focused workflow-scope tests.

Full AWF/GitHub validation is intentionally not run inside this agent phase.
