# PRRT_kwDOSJAM6s6F3vgk Profile Snapshot Realignment Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3vgk_PROFILE_SNAPSHOT_REALIGNMENT_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Preserve the atomic conditional snapshot update; never overwrite an existing `Workspace.resolved_profile`. | Complete | `_persist_resolved_profile_snapshot_if_missing()` still uses one guarded SQL `UPDATE ... WHERE resolved_profile IS NULL OR JSON null`; regression coverage verifies the competing snapshot remains stored. |
| When the conditional update does not apply because a snapshot already exists, reload the frozen snapshot from the workspace row. | Complete | `src/awf/control/executor/state_ops.py` now selects `Workspace.resolved_profile` after a no-op update and returns it. |
| Realign the in-memory `Workspace.resolved_profile` and active `WorkspaceProfile` used by later same-process helpers with the frozen snapshot. | Complete | Added `_profile_from_resolved_profile_snapshot()` in `src/awf/control/executor/helpers.py` and applied it in execution and PR-monitor handoff paths. |
| Preserve current behavior for missing workspace rows and rows whose snapshot is still missing. | Complete | The persistence helper returns `None` when no dict snapshot is available, so callers keep the already resolved runtime profile. |
| Add focused regression coverage for the failed-persist / competing-snapshot path. | Complete | Added `test_runtime_profile_snapshot_returns_competing_snapshot_for_realigning_loser` and `test_profile_from_resolved_snapshot_realigns_workspace_and_active_profile`. |
| Run only targeted validation; full AWF/GitHub validation remains managed by AWF after agent completion. | Complete | Ran focused pytest, Ruff, and touched-module mypy commands only; did not run whole-repo suites, coverage gates, frontend builds, or CI-equivalent validation. |

## Validation Evidence

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_returns_competing_snapshot_for_realigning_loser -q`
  failed with `assert None == competing_snapshot`.
- Passing focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_returns_competing_snapshot_for_realigning_loser tests/unit/control/test_executor_runtime_profile_snapshot.py::test_profile_from_resolved_snapshot_realigns_workspace_and_active_profile -q`
  passed.
- Passing focused module:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  passed with `6 passed`.
- Passing narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/helpers.py src/awf/control/executor/state_ops.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passed.
- Passing touched-module type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/state_ops.py src/awf/control/executor/helpers.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
