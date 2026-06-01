# PRRT_kwDOSJAM6s6GDpjP Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6GDpjP` reports that the merge loop retries the next
allowed merge method after any first `GitHubClientError`. That bypasses the existing
transient/permanent GitHub error handling for cases such as a temporary 502 from
`gh pr merge --squash`, and can merge with a lower-preference method instead of
backing off and retrying the preferred method later.

## Scope

- Limit changes to PR monitor merge-method fallback behavior and focused unit tests.
- Preserve fallback for errors that indicate the attempted merge style is rejected.
- Preserve existing transient GitHub error handling for ordinary `gh pr merge`
  failures.
- Do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- Add a regression showing a transient first merge failure does not try an allowed
  alternative method in the same monitor cycle.
- Keep existing regressions for method-rejection fallback green, including generic
  "this method" rejection text.
- Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so alternative merge
  methods are tried only after merge-method rejection evidence.
- Run focused merge-method tests and focused lint for touched files.

## Implementation Steps

1. Add a focused regression to `tests/unit/runtime/test_pr_monitor_merge_methods.py`.
2. Run the new regression before implementation when practical to confirm it fails.
3. Add a small merge-method fallback predicate in `merge_loop.py` and use it before
   continuing to the next effective method.
4. Re-run focused tests and lint.
5. Record evidence in `plans/PRRT_kwDOSJAM6s6GDpjP_VALIDATION.md`.
