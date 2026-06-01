# Recheck Release Divergence Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F-s0U` reports a stale divergence window in
`sync_release_pr`: the executor counts commits ahead before monitor profile
setup, but setup can take long enough for the source branch to be merged or
reset. The later PR lookup/create currently still uses the stale positive count.

Scope is limited to the release PR handoff path in
`src/awf/control/executor/monitor_handoff.py` and focused unit coverage for that
behavior.

## Requirements Checklist

- Add a regression test proving that `sync_release_pr` rechecks divergence after
  setup and completes as a no-op when the second count is zero.
- Ensure no release PR lookup/create occurs after the second divergence check
  reports zero commits ahead.
- Preserve existing setup failure ordering: setup must still happen before PR
  lookup/create when the initial count is positive.
- Keep validation focused; do not run the broad AWF/GitHub validation suite.
- Commit the fix locally with the review-thread id in the message.

## Implementation Steps

1. Add a focused unit test in the existing sync release PR handoff test class.
2. Run that single test to confirm it fails against current behavior.
3. Update the handoff to run `count_commits_ahead` again after successful setup
   and status recheck, before `find_or_create_release_pr`.
4. If the second count is zero or negative, complete the workspace with the
   existing no-op path.
5. Run the new test plus nearby affected release-sync handoff tests.
6. Record validation evidence in `plans/RECHECK_RELEASE_DIVERGENCE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff::test_rechecks_commits_ahead_after_setup_and_completes_no_op_when_source_catches_up -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff -q`
  - Passes after implementation.

Full AWF/GitHub validation is managed by AWF after agent completion and will not
be executed in this agent phase.
