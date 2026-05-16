# Review Thread PRRT_kwDOSJAM6s6CkE8h Plan

## Problem Statement

Operator refresh requests create `OperationType.refresh` rows in `pending`
status, but the current refresh request path only records
`workspace.refresh_requested` events. No worker path later finishes those
operation rows, so repeated refreshes with the same operator payload can
coalesce into an old active row and appear stuck.

## Scope

- Keep the existing operator refresh behavior: record the refresh-requested
  workspace event used by worker recovery logic.
- Ensure accepted refresh requests do not remain active when no worker-side
  refresh execution is dispatched.
- Preserve exact idempotency-key replay behavior.
- Preserve fresh-key stale `expected_version` conflict behavior.
- Do not change validate or rebase operation lifecycles.

## Requirements Checklist

- [ ] Add or update regression tests showing refresh operations finish
  terminally after the request is accepted.
- [ ] Add or update regression tests showing a second same-payload fresh-key
  refresh after the first completed operation creates a new operation instead
  of coalescing into the old one.
- [ ] Update MCP real-DB expectations so `awf_refresh_workspace` surfaces the
  terminal refresh operation.
- [ ] Finish accepted refresh operations with a structured result that includes
  enough audit context for callers.
- [ ] Run focused tests for the touched service and MCP behavior.

## Implementation Steps

1. Update `tests/unit/service/test_controls_lifecycle.py` refresh tests to
   expect a succeeded operation and to cover repeat refresh creation after the
   previous refresh completed.
2. Update `tests/unit/mcp/test_mcp_control_contracts.py` real-DB refresh
   expectations from `pending` to `succeeded`.
3. Change `WorkspaceControlService.request_refresh_workspace` to finish the
   created refresh operation after writing `workspace.refresh_requested`.
4. Run the focused tests, fix failures, then create the validation document.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_control_contracts.py -q`
