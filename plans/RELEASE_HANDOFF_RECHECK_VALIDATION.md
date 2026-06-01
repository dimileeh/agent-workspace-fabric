# Release Handoff Recheck Validation

Plan reference: `plans/RELEASE_HANDOFF_RECHECK_PLAN.md`

## Requirement Status

- Complete: A prepared-profile handoff still rechecks workspace status before adapter or monitor factory construction.
  - Evidence: `_build_handoff_pr_monitor` now calls `_recheck_status(...)` after optional setup regardless of `run_profile_setup`.
- Complete: The `sync_release_pr` path uses the release handoff action name for this pre-factory stale check.
  - Evidence: `_handoff_sync_release_pr_monitor` passes `stale_action="sync_release_pr_handoff"` when calling `_build_handoff_pr_monitor` with `run_profile_setup=False`.
- Complete: Existing feature handoff setup/recheck behavior remains unchanged.
  - Evidence: Adjacent feature handoff tests passed.
- Complete: Added a regression test that fails before the fix and passes after it.
  - Evidence: `test_release_pr_ready_recheck_blocks_monitor_factory` failed before implementation because the monitor factory was called, then passed after the fix.
- Complete: Ran only targeted checks for the changed behavior.
  - Evidence: Full AWF/GitHub validation was not run in the agent phase; AWF owns that post-agent validation.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py -k "release_pr_ready_recheck_blocks_monitor_factory" -q`
  - Pre-fix result: failed as expected; monitor factory was called.
  - Post-fix result: passed, `1 passed, 21 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -k "release_pr_ready_recheck_blocks_monitor_factory or ahead_creates_release_pr_and_enters_monitoring or ahead_reuses_existing_open_release_pr or recheck_prevents_monitor_run_after_handoff or handoff_setup_status_recheck_blocks_monitor_factory or handoff_monitor_rejects_prepared_profile_with_setup_enabled or sync_feature_pr_handoff_runs_profile_setup_before_monitor" -q`
  - Result: passed, `7 passed, 36 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py`
  - Result: passed.

## Remaining Gaps

None for the planned scope. Broad repository validation, coverage gates, and CI-equivalent checks remain intentionally deferred to AWF/GitHub after agent completion.
