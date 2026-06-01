# Review PRRT_kwDOSJAM6s6GDW-8 Merge-Method Preflight Retry Plan

## Problem Statement and Scope

The PR review thread reports that `GitHubClientError` raised while fetching
repository or branch merge-method rules is recorded as a merge-method mismatch
blocker for the current head. That can permanently stop auto-merge after a
transient GitHub/`gh` API failure.

Scope is limited to the PR monitor merge path and its focused merge-method
regression tests.

## Requirements Checklist

- Add a regression test proving transient merge-method preflight failures use
  the existing transient GitHub retry path.
- Do not persist `__awf_merge_method_blocked__` unless the effective merge
  method set is known to be empty or a merge attempt proves a method is
  disallowed.
- Preserve current merge-method mismatch behavior for proven method
  disallowance.
- Run only focused local checks for the touched behavior; leave broad AWF/GitHub
  validation to AWF after agent completion.

## Implementation Steps

1. Extend the fake merge-method GitHub client in
   `tests/unit/runtime/test_pr_monitor_merge_methods.py` to raise preflight
   errors.
2. Add a focused async regression for a transient branch rules API failure.
3. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so preflight
   `GitHubClientError` is routed through `_wait_after_transient_github_error`
   before any merge-method blocker is recorded.
4. Keep non-transient preflight errors terminal rather than silently converting
   them into a permanent merge-method mismatch.
5. Validate with the focused merge-method test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  must pass.
- Full repository validation, coverage, and CI-equivalent checks are not run in
  the agent phase per the AWF workspace contract.
