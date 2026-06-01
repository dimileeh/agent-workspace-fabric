# Release Handoff Recheck Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F_lvF` reports that `sync_release_pr` handoff prepares the monitor profile before GitHub PR lookup/create, but then calls `_build_handoff_pr_monitor(..., run_profile_setup=False)` after PR creation. The shared builder currently runs the stale workspace-status recheck only when `run_profile_setup=True`, so the release path can build an adapter or PR monitor factory after the workspace has stopped being `running`.

Scope is limited to the release/feature PR monitor handoff stale-status guard and a focused regression test.

## Requirements Checklist

- [ ] A prepared-profile handoff must still recheck workspace status before adapter or monitor factory construction.
- [ ] The `sync_release_pr` path must use the release handoff action name for this pre-factory stale check.
- [ ] Existing feature handoff setup/recheck behavior must remain unchanged.
- [ ] Add a regression test that fails before the fix and passes after it.
- [ ] Run only targeted checks for the changed behavior; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a focused `sync_release_pr` unit regression that lets setup and GitHub PR creation complete, then makes the next release handoff recheck fail before monitor factory construction.
2. Update `_build_handoff_pr_monitor` so the stale-status recheck runs after optional setup regardless of `run_profile_setup`.
3. Pass `stale_action="sync_release_pr_handoff"` from the release handoff caller.
4. Run the targeted regression or narrow test module subset that covers the changed path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py -k "release_pr_ready_recheck_blocks_monitor_factory" -q`
  - Passes after implementation.
- Optionally run the adjacent feature/release handoff setup tests if needed:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -k "handoff_setup or handoff_monitor" -q`

Full repository tests, coverage gates, frontend builds, and CI-equivalent validation are intentionally not run in the agent phase per the AWF workspace contract.
