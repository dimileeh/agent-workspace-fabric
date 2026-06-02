# Comment 4585090228 Review Summary Diagnostics Plan

## Problem Statement And Scope

Address the latest review-level summary for PR comment `issue:4585090228`.
The scope is limited to three host-port/planning auto-retry diagnostics:

- refresh blocked-event detail when a repeated host-port conflict is now blocked
  by a different workspace;
- avoid scanning null-node planning-scope retry candidates on every cleanup
  worker by resolving candidates to one effective node;
- remove the retry latency race where source runtime release is checked before
  the host-port advisory lock.

## Requirements Checklist

- Add focused regression coverage before behavior changes where practical.
- Keep deduplication for unchanged host-port conflicts, but record a new blocked
  event when `detail.conflicting_workspace_id` changes.
- Filter cleanup auto-retry resume candidates by effective node using
  `Workspace.node_id` with resource-reservation fallback, while preserving the
  legacy no-node/no-reservation fallback.
- Acquire the host-port admission lock before the source-runtime release gate in
  `retry_workspace_row` so a concurrent release event is observed under the
  serialized port-admission section.
- Keep validation focused; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add executor regression coverage for changed host-port blocker detail after a
   prior deduped blocked event.
2. Add cleanup query regression coverage for effective-node filtering from a
   resource reservation and the legacy null-node/no-reservation fallback.
3. Add retry-service regression coverage proving the admission lock is acquired
   before the source-runtime release gate.
4. Implement the smallest code changes needed to pass those tests.
5. Run the targeted tests and focused lint/type checks for touched files only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_records_changed_host_port_blocker -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_uses_reservation_effective_node tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_preserves_legacy_no_node_fallback -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_acquires_host_port_lock_before_source_runtime_release_gate -q`
  passes.
- Focused ruff/mypy checks on changed modules pass.

Full AWF/GitHub validation is intentionally not run in the agent phase.
