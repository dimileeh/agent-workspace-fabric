# Comment 4585090228 Review Summary Remaining Plan

## Problem Statement and Scope

Review-level comment `issue:4585090228` reports four remaining planning-scope
auto-retry edge cases:

- repeated post-release resume failures can append duplicate
  `workspace.planning_scope_auto_retry_resume_failed` events on every cleanup
  scan;
- the cleanup candidate query's stale-event guard uses UUID string comparison
  when both `occurred_at` and nullable `event_order` tie;
- `_source_runtime_not_yet_released` relies on the caller having already found
  host ports, but the docstring does not state that precondition;
- post-release retries can keep polling after a third-party host-port conflict
  has replaced the original source-runtime block, which is intentional but
  under-documented.

Scope is limited to bounded event recording, deterministic cleanup candidate
ordering, and clarifying documentation. Do not change host-port admission
semantics or run broad AWF/GitHub validation.

## Requirements Checklist

- Add regression coverage that a latest equivalent `resume_failed` marker is
  not appended again.
- Add regression coverage that a plain manual retry with the same timestamp and
  null `event_order` suppresses a blocked retry candidate without relying on
  UUID lexical ordering.
- Replace UUID-tiebreaker freshness comparison in the cleanup stale-event guard
  with event-order-aware logic that treats unknown same-tick ordering
  conservatively.
- Document the zero-host-port precondition on `_source_runtime_not_yet_released`.
- Document the intentional cleanup-scan retry loop for third-party host-port
  conflicts after source runtime release.
- Run focused pytest/ruff checks only; full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add focused failing tests for duplicate `resume_failed` suppression and
   same-tick null-order manual retry suppression.
2. Update `planning_ops` to inspect the latest terminal-release auto-retry
   event once and skip appending an equivalent `resume_failed` marker.
3. Update `cleanup.py` to avoid UUID lexical freshness tiebreaking in
   `newer_planning_event_exists`; when both event orders are absent at the same
   timestamp, conservatively treat the later terminal event as a suppressor.
4. Add concise doc comments/docstrings for the source runtime host-port
   precondition and the third-party conflict polling behavior.
5. Re-run the focused tests and touched-file lint.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_resume_failure_dedups_latest_equivalent_marker tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_suppresses_same_tick_null_order_manual_retry -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_resume_failure_records_recoverable_event tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_resume_failure_dedups_latest_equivalent_marker tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_suppresses_same_tick_null_order_manual_retry -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/service/workspaces_retry.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py
```

All focused checks must pass. No full unit suite, coverage gate, frontend build,
OpenAPI drift check, push, rebase, or branch switch is part of this fix.
