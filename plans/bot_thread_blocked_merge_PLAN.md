# Bot Thread Blocked Merge Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DXrSS` reports that `decide()` can return
`NotifyHuman` when GitHub reports `BLOCKED` or `HAS_HOOKS` solely because a
bot-authored inline review thread remains open after AWF already triaged it as
`false_positive`. That can stall auto-merge even though comment work is done.

Scope is limited to the PR monitor decision policy and focused regression tests.

## Requirements Checklist

- Preserve the existing rule that newly actionable inline threads route to
  `AddressComments`.
- Preserve the existing rule that unresolved human-deferred feedback routes to
  `NotifyHuman`.
- Allow `BLOCKED` / `HAS_HOOKS` states to reach `Merge` when the only remaining
  unresolved inline thread is bot-authored and already addressed.
- Keep explicit human review blockers unchanged.

## Implementation Steps

1. Add a regression test for `BLOCKED` with a bot-authored inline thread marked
   `false_positive`, expecting `Merge`.
2. Update `decide()` so the `BLOCKED` / `HAS_HOOKS` fallback only notifies a
   human for unresolved inline threads that are not already addressed
   bot-authored feedback.
3. Run the narrow unit test file for `pr_monitor`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor.py -q`
- Pass criteria: the new regression test and existing monitor decision tests pass.
