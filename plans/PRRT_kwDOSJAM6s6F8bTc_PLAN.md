# PRRT_kwDOSJAM6s6F8bTc Plan

## Problem Statement And Scope

The PR monitor comment fix cycle preloads `owned_paths` for prompt generation with
`_owned_paths_for_prompt`. If that database read raises, the exception escapes
`_run_fix_cycle` before the operation can return a handled result, which can crash
the monitor loop while addressing review feedback.

Scope is limited to the prompt-context owned-path prefetch used by
`src/awf/runtime/pr_monitor_runner/fix_cycle.py`.

## Requirements Checklist

- Add a safe owned-path prompt loader that logs degraded prompt context and returns
  an empty list when owned-path loading fails.
- Use the safe loader in `_run_fix_cycle` so comment repair can continue instead
  of propagating transient database failures from prompt-context loading.
- Preserve `_owned_paths_for_prompt` behavior for direct callers and existing
  tests that assert it propagates programming-contract errors.
- Add a focused regression test for the fix-cycle owned-path loading failure.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Add `_owned_paths_for_prompt_or_empty` beside `_owned_paths_for_prompt` in
   `comments.py`.
2. Import and use `_owned_paths_for_prompt_or_empty` in `fix_cycle.py`.
3. Add a unit test that monkeypatches the underlying owned-path loader to raise
   and verifies `_run_fix_cycle` still addresses the comment batch.
4. Run the focused pytest target covering the new regression.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`

Pass criteria: the focused unit test file passes, including the new regression.
