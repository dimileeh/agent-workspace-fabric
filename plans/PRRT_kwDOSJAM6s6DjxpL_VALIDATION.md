# PRRT_kwDOSJAM6s6DjxpL Validation

Plan reference: `PRRT_kwDOSJAM6s6DjxpL_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving replacement salvage for non-running
  `validating`/`pushing` workspaces cancels the superseded active validate/push
  operation.
  - Evidence: Added
    `test_preserved_active_without_usable_work_cancels_superseded_active_operation`
    in `tests/unit/control/test_worker.py`.
  - Red evidence: The focused test failed before the implementation because
    the original operation remained `pending`/`running`.
- Complete: Reuse the existing `_cancel_superseded_active_execution_operations`
  helper so cancellation result metadata remains consistent with validation
  salvage.
  - Evidence: `_create_preserved_active_replacement` now calls the helper with
    the retry operation as `replacement_operation_id` and the preservation event
    as the cycle marker.
- Complete: Preserve the replacement retry operation and replacement workspace
  behavior.
  - Evidence: Existing preserved-active no-work replacement tests passed with
    the new regression.
- Complete: Keep the fix scoped and avoid changing unrelated recovery paths.
  - Evidence: Only `src/awf/control/worker.py` and the focused worker unit test
    file changed.

## Verification Evidence

- Failed before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work_cancels_superseded_active_operation'`
  failed with the original operation still `pending`/`running`.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work_cancels_superseded_active_operation'`
  passed: 4 passed, 237 deselected.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_without_usable_work'`
  passed: 8 passed, 233 deselected.
- Passed after fix:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.

## Remaining Gaps

None.
