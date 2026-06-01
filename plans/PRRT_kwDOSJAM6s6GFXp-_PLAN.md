# PRRT_kwDOSJAM6s6GFXp- Plan

## Problem Statement and Scope

The merge-method preflight fallback posts a direct human notification after a
permanent preflight failure. If that notification post fails transiently, the
exception can escape the merge monitor instead of backing off and continuing.

Scope is limited to the PR monitor merge loop and a focused regression test for
this review thread.

## Requirements Checklist

- Add a regression test where merge-method preflight fails permanently but the
  human-notification comment post fails transiently.
- Ensure the monitor treats that notification failure as transient, waits, and
  returns non-terminal processing instead of raising.
- Preserve existing permanent preflight notification behavior.
- Do not run broad AWF/GitHub-owned validation; record only focused checks.

## Implementation Steps

1. Extend the merge-method unit fake to optionally raise from `post_comment`.
2. Add the failing regression test in `tests/unit/runtime/test_pr_monitor_merge_methods.py`.
3. Wrap the preflight fallback notification post in transient GitHub error
   handling in `src/awf/runtime/pr_monitor_runner/merge_loop.py`.
4. Run the focused test module or selected test cases that cover the regression
   and nearby existing behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  - Passes the new regression and existing merge-method behavior tests.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
