# PRRT_kwDOSJAM6s6JuWDH Plan

## Problem Statement and Scope

GitHub PR creation can lose retry evidence when a retry eventually returns an empty PR URL. The fix is limited to preserving structured retry details on that terminal error path.

## Requirements Checklist

- Verify the inline review against the current `src/awf/runtime/pr_creator.py` implementation.
- Add a focused regression test for transient GitHub PR creation failure followed by an empty URL.
- Preserve accumulated `failures` and `reconcile_lookups` in the raised `PullRequestError.details`.
- Run only targeted validation for the changed behavior; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/runtime/test_pr_creator.py`.
2. Update the GitHub empty-URL terminal path in `src/awf/runtime/pr_creator.py` to attach `_github_pr_create_details(...)`.
3. Re-run the focused unit test.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6JuWDH_VALIDATION.md`.
