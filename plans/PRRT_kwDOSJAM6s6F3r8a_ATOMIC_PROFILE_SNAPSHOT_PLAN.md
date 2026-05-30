# PRRT_kwDOSJAM6s6F3r8a Atomic Profile Snapshot Plan

## Problem Statement And Scope

The PR review thread reports that executor runtime profile snapshot persistence
uses a read-modify-write sequence. Two workers can both observe a missing
`Workspace.resolved_profile`, and the later commit can overwrite the earlier
snapshot, violating the "if missing" contract.

Scope is limited to `src/awf/control/executor/state_ops.py` and focused
regression coverage in the existing executor runtime profile snapshot tests.

## Requirements Checklist

- Replace the read-modify-write snapshot persistence path with one conditional
  SQL update guarded by a missing `Workspace.resolved_profile` predicate.
- Treat both SQL NULL and the JSON-column persisted JSON null value as missing,
  matching the existing Python behavior that loads both as `None`.
- Commit only when the conditional update changes the workspace row.
- Preserve existing behavior for missing workspaces and already-frozen profile
  snapshots.
- Add regression coverage showing a competing snapshot is not overwritten by a
  stale worker.
- Do not run broad AWF/GitHub-owned validation; use targeted checks only.

## Implementation Steps

1. Add a failing regression test to
   `tests/unit/control/test_executor_runtime_profile_snapshot.py`.
2. Update `_persist_resolved_profile_snapshot_if_missing` to use SQLAlchemy
   `update(Workspace)` with `Workspace.id == workspace_id` and
   `Workspace.resolved_profile.is_(None)`.
3. Commit only when the conditional update returns the updated workspace row.
4. Run the focused regression test and the focused test module if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_atomic_update_preserves_competing_snapshot -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  - Passes for the focused snapshot persistence module.

Full AWF/GitHub validation is managed by AWF after agent completion.
