# PRRT_kwDOSJAM6s6GDqov Plan

## Problem Statement and Scope

An unresolved review thread reports that the PR monitor merge loop only tries the first two effective merge methods and can notify humans about a merge-method blocker before trying a remaining allowed method such as `rebase`.

Scope is limited to the merge-method fallback logic in `src/awf/runtime/pr_monitor_runner/merge_loop.py`, its focused unit coverage in `tests/unit/runtime/test_pr_monitor_merge_methods.py`, and this plan/validation record.

## Requirements Checklist

- Add a regression test showing `squash` and `merge` method failures still allow a third permitted `rebase` attempt.
- Preserve existing transient failure behavior: transient merge errors must not switch methods.
- Preserve existing permanent blocker behavior when no allowed alternatives remain.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for the touched behavior.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Inspect the current merge-method loop and merge-method unit tests.
2. Add a failing unit test for three allowed methods where the first two method-specific attempts fail and the third succeeds.
3. Update the merge loop to iterate all effective allowed methods and only record a merge-method blocker after alternatives are exhausted.
4. Run the focused merge-method unit tests.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GDqov_VALIDATION.md`.
6. Stage only changed files and create a conventional commit for this review thread.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`

Pass criteria: the focused merge-method test module passes, including the new regression, and no broad validation suite is run inside the agent phase.
