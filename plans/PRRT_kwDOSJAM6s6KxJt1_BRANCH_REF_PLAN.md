# PRRT_kwDOSJAM6s6KxJt1 Branch Ref Plan

## Problem Statement and Scope

The missing-HEAD recovery path resolves the current worktree branch ref after an
agent operation and then rewrites that ref in the shared mirror. If the agent
changed the worktree to another local branch, recovery could mutate the wrong
mirror ref.

Scope is limited to validating the recovered worktree branch ref before mirror
mutation in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and covering
that behavior with focused unit tests.

## Requirements Checklist

- Fail closed before `git update-ref` when the resolved worktree ref differs
  from the workspace's expected local branch ref.
- Preserve existing missing-HEAD recovery behavior when the resolved ref matches
  the workspace branch.
- Keep the fix scoped to the PR monitor recovery path.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Add a regression test for mismatched worktree branch ref during missing-HEAD
   filesystem recovery.
2. Add a helper to resolve the expected workspace local branch ref from
   `Workspace.branch_name`.
3. Thread that expected ref into recovery and compare it before mirror
   `update-ref`.
4. Update any affected test doubles for the new helper signature.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_updates_expected_branch_ref -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Pass criteria: the focused branch-ref recovery tests pass and lint passes for
the touched Python files.

## Assumptions/Changes

An attempted run of the full
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
file exposed an unrelated existing failure in
`test_pre_push_validation_fix_pass_recovers_missing_head`, where that test's
fixture does not mock `remote_repair.repair_mirror_hooks_path` for the nested
`_commit_dirty_worktree` path. This branch-ref review thread is validated with
the narrower branch-ref tests above plus focused lint.
