# PRRT_kwDOSJAM6s6GDXIj Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6GDXIj` reports that `merge_loop.py` skips the second allowed merge method unless GitHub's rejection classifier returns the same method that AWF just attempted. This can leave the monitor retrying the same failing method on later polls instead of attempting the next effective method in the same merge cycle.

## Scope

- Limit code changes to PR monitor merge-method retry behavior and focused unit tests.
- Preserve existing behavior for a single effective method: notify a human for classified merge-method mismatches and use existing transient/non-transient GitHub error handling for other failures.
- Do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- Add a regression test showing an unclassified first merge failure still retries the second effective method.
- Add a regression test showing a classified-but-mismatched first merge failure still retries the second effective method.
- Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so the first failed attempt tries the next effective method whenever a second effective method exists.
- Preserve human notification for final classified merge-method rejection.
- Run focused unit tests for merge-method behavior only.

## Implementation Steps

1. Add focused tests to `tests/unit/runtime/test_pr_monitor_merge_methods.py`.
2. Run the new tests before implementation when practical to confirm the reported failure.
3. Change the merge-attempt loop to continue after the first failed attempt when another effective method is available.
4. Re-run focused merge-method tests.
5. Document validation evidence in `plans/PRRT_kwDOSJAM6s6GDXIj_VALIDATION.md`.
