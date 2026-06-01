# Review Issue 4590903660 Method-Blocker Reason Plan

## Problem Statement and Scope

The review-level comment reports that `_MergeAttemptResult.notification_reason`
can be `None` even when the outcome is `METHOD_BLOCKER`. The current merge path
constructs a reason before returning that outcome, but the dataclass contract
does not enforce it, and the caller assigns the optional field directly before
the next-poll `decide()` state check depends on a truthy stored value.

Scope is limited to the merge-method attempt result invariant, its caller in
`merge_loop.py`, a focused regression test, and this plan/validation pair.

## Requirements Checklist

- Enforce that `METHOD_BLOCKER` merge-attempt results always include a
  non-empty notification reason.
- Keep `notification_reason` optional for success, retry-next-method, and
  ordinary blocker outcomes.
- Update the merge-loop caller to consume the method-blocker reason through a
  non-optional contract.
- Run focused validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add a dataclass invariant and typed accessor to `_MergeAttemptResult`.
2. Use the accessor in the `METHOD_BLOCKER` branch of the merge loop.
3. Add focused unit coverage for the invariant and allowed non-method-blocker
   defaults.
4. Run targeted tests and lint for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passes.
- Full AWF/GitHub validation, whole-repository tests, and coverage gates are
  not run locally per the AWF workspace contract.
