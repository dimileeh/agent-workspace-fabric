# Monitor Resume Profile Sync Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F4oae` reports that `resume_pr_monitor`
can keep a non-null local runtime profile after `_sync_resolved_profile` fails,
then skip the later monitor-factory sync retry because the local profile is not
`None`. The fix is scoped to monitor resume profile persistence and its
regression coverage.

## Requirements Checklist

- Add a regression test for a monitor resume where the first
  `_sync_resolved_profile` call fails after profile resolution, and the second
  call succeeds before monitor construction.
- Ensure the monitor factory receives the persisted/synced profile, not an
  unsynced local profile.
- Keep compose restart behavior unchanged; sync failure during timeout
  resolution remains non-terminal until monitor construction retries.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for the
  changed behavior.

## Implementation Steps

1. Add the failing regression in existing monitor resume unit coverage.
2. Confirm the targeted test fails on the current implementation.
3. Update `resume_pr_monitor` so a failed initial sync does not leave the local
   `profile` variable looking ready for monitor construction.
4. Run the targeted regression test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q -k retries_profile_sync`
  - Passes only after the implementation retries sync and uses the synced
    profile for the monitor factory.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per workspace contract.
