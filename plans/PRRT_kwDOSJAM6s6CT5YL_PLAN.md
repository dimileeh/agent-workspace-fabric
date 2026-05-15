# PRRT_kwDOSJAM6s6CT5YL Plan

## Problem Statement and Scope

An unresolved review thread reports that `_run_fix_cycle` can mark earlier review
items as addressed, then return early on `ProtectedScopeDiffError` before the
normal failed-push cleanup clears those in-memory addressed markers. This can
leave `MonitorState.threads_addressed_ids` suppressing re-queue of items whose
fix was never published.

Scope is limited to the protected-scope early returns inside `_run_fix_cycle`
for inline review threads and review-level comments.

## Requirements Checklist

- Add regression coverage for an inline review thread addressed earlier in a
  fix pass when a later thread hits `ProtectedScopeDiffError`.
- Add regression coverage for a review-level comment addressed earlier in a fix
  pass when a later review comment hits `ProtectedScopeDiffError`.
- Clear all publish-dependent addressed state before returning the protected
  scope diff unavailable push result.
- Preserve existing protected-scope failure behavior and reason codes.
- Keep changes narrow to the monitor runner and focused unit tests.

## Implementation Steps

1. Add failing tests that monkeypatch the second item in `_run_fix_cycle` to
   raise `ProtectedScopeDiffError` and assert the first item's addressed state
   and body-hash marker are cleared.
2. Update the two `_run_fix_cycle` `ProtectedScopeDiffError` handlers to clear
   `publish_dependent_ids` via `_clear_addressed_state_by_id` before returning.
3. Run the new tests, then run the focused unit test file if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  must pass.
