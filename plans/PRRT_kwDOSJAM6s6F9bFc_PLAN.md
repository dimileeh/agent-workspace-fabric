# PRRT_kwDOSJAM6s6F9bFc Plan

## Problem Statement And Scope

The review thread reports that `sync_release_pr` handoff can create or adopt a
release PR before monitor handoff profile setup runs. If setup then fails, AWF
marks the workspace failed while leaving the newly created/adopted release PR
unmonitored. The fix must keep release PR creation behind successful setup for
workspaces that have commits to sync.

Scope is limited to the release-sync handoff path in
`src/awf/control/executor/monitor_handoff.py`, focused unit coverage, and this
plan/validation documentation.

## Requirements Checklist

- Confirm no-op release-sync workspaces still complete without setup, PR
  creation, or monitor execution.
- For release-sync workspaces with commits ahead, run monitor handoff profile
  setup before creating or adopting a PR.
- If setup fails, fail the workspace without running any `gh pr list`,
  `gh pr create`, or PR metadata adoption commands.
- Preserve existing release-sync failure handling and monitoring persistence for
  successful setup.
- Do not run broad AWF/GitHub validation; use focused tests only.

## Implementation Steps

1. Add a regression test that queues commits ahead and failing setup, then
   asserts no GitHub PR commands are executed.
2. Split the release-sync executor flow so it counts commits first, exits early
   for no-op, runs the existing monitor handoff setup/build path, rechecks the
   workspace status, and only then creates/adopts the release PR.
3. Keep existing exception-to-failure mappings for release sync Git and GitHub
   errors.
4. Run focused tests for the release-sync handoff class.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff -q`

Pass criteria: the focused release-sync handoff tests pass. Full AWF/GitHub
validation remains managed by AWF after agent completion.
