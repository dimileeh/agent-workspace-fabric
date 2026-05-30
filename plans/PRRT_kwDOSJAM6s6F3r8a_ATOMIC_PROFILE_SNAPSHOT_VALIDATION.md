# PRRT_kwDOSJAM6s6F3r8a Atomic Profile Snapshot Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3r8a_ATOMIC_PROFILE_SNAPSHOT_PLAN.md`

## Requirement Status

- Replace read-modify-write with one conditional SQL update: Complete.
  `_persist_resolved_profile_snapshot_if_missing` now uses a guarded
  `UPDATE workspaces ... RETURNING workspaces.id`.
- Commit only when the conditional update changes the workspace row: Complete.
  The helper commits only when the guarded update returns a workspace id.
- Preserve missing-workspace and already-frozen snapshot behavior: Complete.
  Missing rows return no update result; existing profile snapshots do not match
  the missing-profile predicate.
- Cover SQL NULL and JSON null missing values: Complete. The predicate covers
  both SQL NULL and the JSON-column persisted `null` representation that loads
  as Python `None`.
- Add regression coverage for the race: Complete. The new focused regression
  simulates a competing snapshot landing before a stale worker can commit and
  asserts the stale worker does not overwrite it.
- Avoid broad AWF/GitHub-owned validation: Complete. Only focused tests and
  targeted lint/type checks were run.

## Evidence

- Initial regression check before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_atomic_update_preserves_competing_snapshot -q`
  failed because the old code did not issue the atomic update.
- Focused module:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  passed with 4 tests.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/state_ops.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passed.
- Targeted type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/state_ops.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
