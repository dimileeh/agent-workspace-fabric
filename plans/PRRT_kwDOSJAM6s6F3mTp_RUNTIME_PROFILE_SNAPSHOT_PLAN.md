# Runtime Profile Snapshot Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6F3mTp` reports that merge-queue blocker checks filter
custom planning artifacts from `Workspace.resolved_profile`, but auto-profile
workspaces can reach runtime without a persisted resolved profile snapshot. When
that happens, custom generated plan paths such as `docs/alternate/ws_*.md` are
treated as ordinary owned paths and unrelated merge candidates can block each
other.

Scope is limited to preserving the runtime-resolved profile snapshot for
workspaces that still lack one, so existing merge-queue filtering can use the
same resolved profile data as inline and explicit profile-ref workspaces.

## Requirements Checklist

- Persist the runtime-resolved profile snapshot when an executor resolves a
  workspace profile and the workspace row still has no `resolved_profile`.
- Preserve existing immutable snapshots; do not overwrite workspaces that
  already have `resolved_profile`.
- Keep in-memory workspace objects aligned with the runtime-resolved snapshot so
  later same-process helpers see the profile too.
- Add focused regression coverage for missing-snapshot runtime persistence.
- Run only targeted validation; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a small executor state helper that stores `profile.model_dump(...)` into
   `Workspace.resolved_profile` only when the database value is currently null.
2. Update `_profile_for_workspace()` to attach the resolved snapshot to the
   current workspace object when it resolved from runtime inputs.
3. Call the persistence helper after runtime profile resolution during executor
   execution, and from PR-monitor handoff/profile-build paths that also resolve
   profiles at runtime.
4. Add a targeted unit test proving an auto-profile workspace with no snapshot
   gets the runtime profile persisted and an existing snapshot is not replaced.
5. Run the new focused test and a narrow merge-queue custom artifact regression
   test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor tests/unit/control/test_executor_runtime_profile_snapshot.py`

Pass criteria: all targeted commands pass; no broad validation suite or coverage
gate is run in the agent phase.
