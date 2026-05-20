# PRRT_kwDOSJAM6s6Dckhq Plan

## Problem Statement And Scope

The active-execution salvage replacement path in `src/awf/control/worker.py`
creates a replacement workspace without preserving the source workspace's
`remote_push_branch`. For monitor/sync task kinds this can lose the external PR
head branch and cause later pushes to target the replacement local branch.

Scope is limited to the worker salvage replacement behavior and a focused
regression test.

## Requirements Checklist

- Preserve `remote_push_branch` when active-execution salvage creates a
  replacement for monitor/sync task kinds that depend on an external remote
  branch.
- Keep ordinary `feature_branch_pr` replacement behavior unchanged so fresh
  replacements do not reuse the source feature branch.
- Add a regression test that fails before the implementation change.
- Run the narrowest relevant test proving the behavior.

## Implementation Steps

1. Add a focused worker regression test for a `sync_release_pr` preserved-active
   replacement with a source `remote_push_branch` different from `branch_name`.
2. Run that test to confirm it fails against current behavior.
3. Update the worker replacement creation path to pass the source
   `remote_push_branch` only for monitor/sync task kinds.
4. Re-run the focused test and a nearby feature-branch replacement test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work'`
  passes.
- The existing feature-branch replacement assertion continues to expect
  `replacement.remote_push_branch is None`.
