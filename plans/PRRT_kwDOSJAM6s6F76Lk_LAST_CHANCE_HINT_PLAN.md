# PRRT_kwDOSJAM6s6F76Lk Last-Chance Hint Plan

## Problem And Scope

An inline review reports that `handle_merge_action` can refresh persisted operator hint/freeze state, pass final merge gates, then call `merge_pr` without another database read. A remonitor hint persisted after that refresh can therefore be ignored by the auto-merge path.

Scope is limited to the PR monitor merge loop and its operator-hint merge-recheck regression coverage.

## Requirements

- Add a regression test that persists an operator hint immediately after the existing final operator-state refresh and proves `gh pr merge` is not called.
- Add a last-chance operator-state refresh after the existing final refresh and before beginning the merge attempt.
- Preserve existing freeze/remonitor behavior by re-running initial-review-grace and non-check-reviewer settle checks when the last-chance refresh changes state without changing the action away from `Merge`.
- Keep validation focused to the targeted regression test file; do not run full AWF/GitHub-owned validation.

## Implementation Steps

1. Update `tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py` with the failing last-chance operator hint regression.
2. Run the targeted test and confirm it fails against the current implementation.
3. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` to perform a last-chance operator-state refresh before starting the merge operation.
4. Run the targeted test file or targeted tests needed to prove the fix.
5. Create `plans/PRRT_kwDOSJAM6s6F76Lk_LAST_CHANCE_HINT_VALIDATION.md` with requirement status and focused evidence.
