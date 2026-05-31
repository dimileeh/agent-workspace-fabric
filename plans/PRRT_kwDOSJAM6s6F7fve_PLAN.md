# PRRT_kwDOSJAM6s6F7fve Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F7fve` reports that terminal workflow-scope
push handling in `src/awf/runtime/pr_monitor_runner/loop.py` can be skipped if
the human notification comment fails. The fix is scoped to the terminal
sync-base and CI-repair workflow-scope branches called out by the review.

## Requirements Checklist

- Add focused regression coverage proving sync-base workflow-scope push failures
  still terminate the workspace when posting the human notification fails.
- Add focused regression coverage proving CI-repair workflow-scope push failures
  still terminate the workspace when posting the human notification fails.
- Keep the workflow-scope blocker operation and audit event recorded before
  terminal failure.
- Do not change the intentionally non-terminal comment-repair workflow-scope
  behavior.
- Run only targeted tests for the changed behavior. Full AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Add failing unit tests beside the existing sync-base and CI-repair
   workflow-scope terminal tests.
2. Update terminal workflow-scope notification handling so notification posting
   is best-effort and cannot prevent `_terminate_failed`.
3. Run the targeted tests that cover the changed branches.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6F7fve_VALIDATION.md`.
