# PRRT_kwDOSJAM6s6GDqo1 Plan

## Problem Statement and Scope

PR #353 has an unresolved review thread on
`src/awf/runtime/pr_monitor_runner/merge_loop.py` reporting that the merge
monitor can keep retrying merge API calls when the last configured merge method
fails with a method-disallowed error whose parsed method is mismatched or
unclassified.

Scope is limited to merge-method rejection handling in the PR monitor merge loop
and its focused regression tests.

## Requirements Checklist

- Reproduce the reviewed behavior with focused regression coverage.
- Preserve retries across remaining allowed merge-method alternatives.
- When no allowed alternatives remain, record the merge-method blocker for
  permanent method-related rejections even if the parsed rejected method differs
  from the attempted method or is unclassified.
- Preserve transient GitHub merge failures as retry/backoff without recording a
  merge-method blocker.
- Do not run AWF/GitHub-owned broad validation; record focused validation only.

## Implementation Steps

1. Add focused tests in `tests/unit/runtime/test_pr_monitor_merge_methods.py`
   for last-attempt mismatched and unclassified method-related rejections.
2. Confirm the new regression fails before implementation when practical.
3. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so exhausted
   method-related rejections mark the merge-method blocker independently of
   exact parsed-method equality.
4. Run the focused merge-method test module.
5. Create the validation document with requirement-by-requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`

Pass criteria: the focused test module passes, and no broad AWF/CI validation is
run inside the agent phase.
