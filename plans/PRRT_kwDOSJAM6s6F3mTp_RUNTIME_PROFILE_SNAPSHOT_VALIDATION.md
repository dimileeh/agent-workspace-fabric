# Runtime Profile Snapshot Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3mTp_RUNTIME_PROFILE_SNAPSHOT_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Persist the runtime-resolved profile snapshot when an executor resolves a workspace profile and the workspace row still has no `resolved_profile`. | Complete | Added `_persist_resolved_profile_snapshot_if_missing()` in `src/awf/control/executor/state_ops.py` and call sites in execution and PR-monitor handoff paths. |
| Preserve existing immutable snapshots; do not overwrite workspaces that already have `resolved_profile`. | Complete | The helper returns when the row already has a Python-level `resolved_profile`; regression coverage asserts a frozen snapshot is unchanged. |
| Keep in-memory workspace objects aligned with the runtime-resolved snapshot so later same-process helpers see the profile too. | Complete | `_profile_for_workspace()` now assigns the resolved profile JSON snapshot to `ws.resolved_profile` after runtime resolution; regression coverage asserts this. |
| Add focused regression coverage for missing-snapshot runtime persistence. | Complete | Added `tests/unit/control/test_executor_runtime_profile_snapshot.py`. |
| Run only targeted validation; full AWF/GitHub validation remains managed by AWF after agent completion. | Complete | Ran focused tests, ruff, and a touched-file mypy check only. |

## Evidence

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  failed during collection because `_persist_resolved_profile_snapshot_if_missing`
  did not exist yet.
- Green regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  passed: `3 passed`.
- Merge-queue custom artifact regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate -q`
  passed: `1 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passed.
- Focused format check:
  `uv run --python 3.12 --extra dev ruff format --check tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/state_ops.py src/awf/control/executor/helpers.py src/awf/control/executor/mixins.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py`
  passed.

Full AWF/GitHub validation, broad test suites, and coverage gates were not run
inside the agent phase; AWF owns those after completion.
