# PRRT_kwDOSJAM6s6F6wEU Operator Hint Memory Plan

## Problem Statement and Scope

An unresolved PR review thread reports that `_persist_state()` preserves the DB
processed marker for a concurrently handled operator hint, but leaves the
current `MonitorState.pending_operator_hint` pending in memory. Scope is limited
to the PR monitor runner operator-hint persistence race and its regression test.

## Requirements Checklist

- Add a regression test showing that a stale monitor state clears its in-memory
  pending operator hint when the database already contains the matching
  processed marker.
- Preserve the existing persisted-state merge behavior for concurrent processed
  markers and unrelated addressed threads.
- Keep validation focused; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Extend the existing concurrent processed operator-hint persistence test with
   an assertion on the in-memory state after `_persist_state()`.
2. Run the narrow test to confirm the current bug.
3. Update lifecycle persistence so the DB processed marker clears the matching
   in-memory `pending_operator_hint` and records the processed marker locally.
4. Re-run the targeted test and a focused lint check for touched Python files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_persist_state_preserves_concurrent_processed_operator_hint_marker -q`
  - Fails before the fix on the new in-memory assertion.
  - Passes after the fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passes without new lint findings.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passes without formatting drift.
