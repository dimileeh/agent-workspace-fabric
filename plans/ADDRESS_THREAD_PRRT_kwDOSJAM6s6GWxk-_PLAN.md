# Address Thread PRRT_kwDOSJAM6s6GWxk-

## Problem Statement And Scope

The planning-scope auto-retry path currently records
`WorkspaceCreateHostPortConflictError` as `workspace.planning_scope_auto_retry_failed`.
That makes the retry terminal even though the conflict is caused by another
non-terminal workspace that can later release the disputed port.

Scope is limited to making host-port conflicts resumable for planning-scope
auto-retry, while leaving duplicate host-port requests and generic retry errors
as terminal failures.

## Requirements Checklist

- Add regression coverage showing planning-scope auto-retry host-port conflicts
  record a pending blocked event, not a terminal failed event.
- Preserve existing terminal failed handling for `WorkspaceRetryError` and
  `WorkspaceCreateDuplicateHostPortError`.
- Include retry metadata that lets the cleanup worker find the source workspace
  through the existing pending terminal-release scan.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for touched
  behavior.

## Implementation Steps

1. Update focused tests in planning auto-retry transaction coverage and executor
   edge coverage to expect host-port conflicts to remain resumable.
2. Run the focused tests and confirm the current implementation fails.
3. Change `_request_planning_scope_auto_retry` so
   `WorkspaceCreateHostPortConflictError` records a blocked pending event with
   conflict details and `retry_after=terminal_runtime_released`.
4. Re-run the focused tests and fix any narrow regressions.
5. Save validation evidence in a matching validation document.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
