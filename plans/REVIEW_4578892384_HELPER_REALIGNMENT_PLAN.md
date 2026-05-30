# Review 4578892384 Helper Realignment Plan

## Problem Statement And Scope

Address the remaining review-level maintainability feedback from PR comment
`issue:4578892384`.

Scope is limited to:

- making the executor helper's detached `Workspace.resolved_profile` mutation
  visible at call sites;
- documenting why custom planning templates without `{workspace_id}` remain
  ordinary blocking owned paths;
- updating nearby focused tests/imports affected by the private helper rename.

No runtime behavior change is intended.

## Requirements Checklist

- Rename `_profile_from_resolved_profile_snapshot()` so the name advertises
  profile realignment and the detached workspace mutation.
- Preserve the existing warning in the helper docstring about not adding or
  merging the mutated detached ORM object.
- Update call sites and focused tests to use the new helper name.
- Add an inline comment explaining that fixed custom plan paths are shared
  repository paths and should remain inter-workspace blockers.
- Avoid broad AWF/GitHub-owned validation; run only focused tests and lint for
  changed files.

## Implementation Steps

1. Rename the private helper in `src/awf/control/executor/helpers.py`.
2. Update `src/awf/control/executor/state_ops.py` and the focused executor
   snapshot tests to import/call the renamed helper.
3. Add the fixed-template design comment in `src/awf/common/owned_paths.py`.
4. Run focused tests covering the renamed helper and owned-path classifier.
5. Run targeted ruff for the changed Python files.
6. Record evidence in
   `plans/REVIEW_4578892384_HELPER_REALIGNMENT_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_realign_profile_from_resolved_snapshot_realigns_workspace_and_active_profile tests/unit/common/test_owned_paths.py::test_disabled_planning_custom_profile_paths_are_not_internal_artifacts -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/helpers.py src/awf/control/executor/state_ops.py src/awf/common/owned_paths.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
