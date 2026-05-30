# PRRT_kwDOSJAM6s6F3vgk Profile Snapshot Realignment Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F3vgk` reports that a worker can resolve a
runtime profile locally, assign it to the in-memory `Workspace`, then lose the
conditional snapshot persist race because another worker already froze
`Workspace.resolved_profile`. The losing worker then continues with an
in-memory profile that differs from the persisted immutable snapshot.

Scope is limited to executor profile snapshot persistence and the executor /
PR-monitor call sites that immediately consume the resolved profile. The fix
must preserve the existing "persist only if missing" behavior and avoid broad
AWF/GitHub-owned validation in the agent phase.

## Requirements Checklist

- Preserve the atomic conditional snapshot update; never overwrite an existing
  `Workspace.resolved_profile`.
- When the conditional update does not apply because a snapshot already exists,
  reload the frozen snapshot from the workspace row.
- Realign the in-memory `Workspace.resolved_profile` and active
  `WorkspaceProfile` used by later same-process helpers with the frozen
  snapshot.
- Preserve current behavior for missing workspace rows and rows whose snapshot
  is still missing.
- Add focused regression coverage for the failed-persist / competing-snapshot
  path.
- Run only targeted validation; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a focused failing regression in
   `tests/unit/control/test_executor_runtime_profile_snapshot.py` proving a
   losing worker observes the competing frozen snapshot from the persistence
   helper.
2. Update `_persist_resolved_profile_snapshot_if_missing` to return the frozen
   snapshot after the helper runs: the inserted snapshot when the conditional
   update wins, the reloaded row snapshot when it loses, or `None` when no
   snapshot is available.
3. Add a small helper to rebuild a `WorkspaceProfile` from a persisted snapshot
   and assign that snapshot back onto the in-memory `Workspace`.
4. Update executor and PR-monitor profile-resolution call sites to use the
   returned snapshot to realign their local `profile` variable and workspace
   object.
5. Run focused tests and a narrow lint check for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_returns_competing_snapshot_for_realigning_loser -q`
  - Fails before the implementation and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  - Passes for the focused snapshot persistence module.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/helpers.py src/awf/control/executor/state_ops.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  - Passes for the touched Python files.

Full AWF/GitHub validation is managed by AWF after agent completion.
