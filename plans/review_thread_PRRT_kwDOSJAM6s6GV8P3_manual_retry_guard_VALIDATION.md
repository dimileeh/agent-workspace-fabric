# Review Thread PRRT_kwDOSJAM6s6GV8P3 Manual Retry Guard Validation

Plan reference:
`plans/review_thread_PRRT_kwDOSJAM6s6GV8P3_manual_retry_guard_PLAN.md`

## Requirement Status

- Treat `workspace.retry_requested` as a terminal planning-scope auto-retry
  release event for the same workspace even when its payload lacks
  `source_reason_code`: Complete.
- Keep other planning-scope auto-retry event types scoped to
  `AGENT_PLAN_PHASE_SCOPE_VIOLATION`: Complete.
- Ensure the worker candidate SQL ranks a newer manual retry event ahead of an
  older blocked/resume-failed event, so no stale candidate is returned:
  Complete.
- Preserve the existing pending/resolved semantics for blocked, failed,
  requested, skipped, and resume-failed auto-retry events: Complete.
- Add focused regression coverage before implementation: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/planning_ops.py`
- `src/awf/control/worker/cleanup.py`
- `tests/unit/control/test_executor_planning_auto_retry_transactions.py`
- `tests/unit/control/test_worker_parts/test_worker_part_042.py`

Focused failing-before-fix regression run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_runtime_not_released_skips_blocked_event_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry -q
```

Result before implementation: failed as expected. The executor wrote a stale
`workspace.planning_scope_auto_retry_blocked` event and the worker resumed the
blocked auto-retry candidate after a newer plain manual retry event.

Focused passing regression run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_runtime_not_released_skips_blocked_event_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry -q
```

Result after implementation: `2 passed in 3.28s`.

Neighboring focused regression run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_runtime_not_released_skips_blocked_event_after_manual_retry tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_runtime_not_released_skips_blocked_event_after_plain_manual_retry tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_resumes_blocked_planning_scope_auto_retry tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_pending_check_requires_latest_blocked_event tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node -q
```

Result: `7 passed in 7.79s`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py
```

Result: `All checks passed!`

Full AWF/GitHub validation was not run in the agent phase because the workspace
contract assigns broad validation, provenance, logs, timeouts, and merge gating
to AWF/GitHub after agent completion.
