# Review Thread PRRT_kwDOSJAM6s6GV8P3 Manual Retry Guard Plan

## Problem Statement and Scope

The planning-scope auto-retry terminal-release guard treats
`workspace.retry_requested` as a terminal event, but both the latest-event scan
and the worker candidate query currently require
`source_reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION`. Plain manual retry
events are authored by the retry service and may not carry that synthetic
planning-scope payload field. A newer manual retry can therefore be invisible
to the guard, allowing a stale blocked marker or duplicate resume attempt.

Scope is limited to:

- `src/awf/control/executor/planning_ops.py`
- `src/awf/control/worker/cleanup.py`
- Focused regression tests for the executor guard and worker release scan.

## Requirements Checklist

- Treat `workspace.retry_requested` as a terminal planning-scope auto-retry
  release event for the same workspace even when its payload lacks
  `source_reason_code`.
- Keep other planning-scope auto-retry event types scoped to
  `AGENT_PLAN_PHASE_SCOPE_VIOLATION`.
- Ensure the worker candidate SQL ranks a newer manual retry event ahead of an
  older blocked/resume-failed event, so no stale candidate is returned.
- Preserve the existing pending/resolved semantics for blocked, failed,
  requested, skipped, and resume-failed auto-retry events.
- Add focused regression coverage before implementation.

## Implementation Steps

1. Add an executor regression for a `workspace.retry_requested` event with the
   normal manual retry payload shape and confirm it suppresses blocked marker
   creation after the rollback/re-lock window.
2. Add a worker regression where a blocked planning-scope auto-retry event is
   followed by a plain manual retry event and confirm the release scan does not
   resume the blocked auto-retry.
3. Update the executor latest-event matcher to match manual retry events by
   event type and other terminal events by planning-scope payload.
4. Update the worker candidate SQL to include manual retry events in the ranked
   terminal event stream without requiring `source_reason_code`.
5. Run only focused tests for the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_runtime_not_released_skips_blocked_event_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry -q`

Pass criteria:

- The focused regressions fail before the implementation change.
- The focused regressions pass after the implementation change.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
