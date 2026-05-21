# PRRT_kwDOSJAM6s6DYJyy Unreserved Active Capacity Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DYJyy` reports that the worker's local-capacity
gate initializes allocated resources only from active resource reservations. If
an active local workspace predates reservations or lost its active reservation
during repair, the scheduler can undercount local CPU, memory, and DinD usage
and admit more requested work than the node capacity permits.

Scope is the capacity-gated requested-workspace claim path in
`src/awf/control/worker.py` and focused worker regression coverage.

## Requirements Checklist

- Add a regression proving an active local workspace without an active
  reservation contributes default worker resources to the capacity baseline.
- Preserve latest active reservation accounting for workspaces that already have
  reservations.
- Keep node scoping for allocated capacity: count active workspaces on the
  worker's node and legacy rows with no `Workspace.node_id`, but not rows owned
  by another node.
- Do not include requested candidates in the allocated baseline.
- Commit the scoped fix locally with a conventional commit message for the
  review thread.

## Implementation Steps

1. Add a failing unit test near the existing requested-capacity gate tests.
2. Add a small worker helper that finds active allocated-status workspaces with
   no active reservation row and adds default resource demand for local/null-node
   rows.
3. Use that helper immediately after loading persisted active reservation totals
   in `_claim_requested_ids_with_capacity`.
4. Run the narrow regression, then the relevant worker unit surface and static
   checks if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<regression> -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes or any unrelated failure is documented.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes or any unrelated failure
  is documented.
