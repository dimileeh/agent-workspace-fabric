# Review Issue 4590903660 Merge-Method Test Fixture Plan

## Problem Statement and Scope

The review-level comment reports that
`tests/unit/runtime/test_pr_monitor_merge_methods.py` hard-codes
project-specific repository and branch names in the `_MergeMethodClient` test
double. Those assertions are not part of the merge-method behavior under test
and can make otherwise generic PR-monitor tests fail with misleading assertion
errors.

Scope is limited to the focused merge-method regression test fixture and its
plan/validation notes. The merge-loop runtime behavior was already updated on
this branch to route transient merge-method preflight errors through the
existing retry path, and this plan does not broaden that implementation.

## Requirements Checklist

- Replace project-specific repository and branch literals in the merge-method
  test double with neutral, injected expectations.
- Preserve fixture assertions that the merge loop passes the expected
  repository, PR number, base branch, and delete-branch flag.
- Keep focused merge-method behavior tests intact, including the transient
  preflight regression already present on this branch.
- Run only focused local checks for the touched test file; leave broad
  AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add neutral test constants for repo, PR number, and base branch names.
2. Update `_MergeMethodClient` to accept expected repo/pr/branch values and use
   them for assertions.
3. Update `_execute_merge` and affected tests to use the neutral constants.
4. Run the focused merge-method test file and a focused lint check for the
   touched test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_merge_methods.py`
  must pass.
- Full repository validation, coverage, and CI-equivalent checks are not run in
  the agent phase per the AWF workspace contract.
