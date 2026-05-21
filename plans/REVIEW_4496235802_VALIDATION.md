# Review 4496235802 Validation

Plan reference: `plans/REVIEW_4496235802_PLAN.md`

## Requirement Status

- Complete: `_stale_active_execution_can_fail` treats `running`, `validating`,
  and `pushing` consistently when an active preserved validation recovery can
  continue.
  - Evidence: `src/awf/control/worker.py` early guard now includes
    `WorkspaceStatus.pushing`.
  - Evidence: new regression
    `test_stale_active_execution_check_blocks_pushing_candidate_with_active_validation_recovery`.

- Complete: `_preserved_active_worktree_path` returns `None` only for
  provisioners that lack `get_worktree_path`, while internal `AttributeError`
  raised by the implementation propagates.
  - Evidence: `src/awf/control/worker.py` now uses `getattr_static` for the
    missing-method compatibility check before invoking the method.
  - Evidence: `test_preserved_active_unavailable_worktree_root_classifies_as_ambiguous`
    covers missing-method compatibility.
  - Evidence:
    `test_preserved_active_worktree_path_propagates_internal_attribute_error`
    covers the internal-bug case.

- Complete: Existing ambiguous `worktree_root_unavailable` behavior remains
  covered for provisioners without a worktree-path method.

- Complete: Regression tests were written before implementation and confirmed
  failing.
  - Evidence: targeted pytest failed before the worker change with the new
    `pushing` stale-cleanup assertion and internal `AttributeError` propagation
    assertion.

- Complete: Narrow validation passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'stale_active_execution_check_blocks_pushing_candidate_with_active_validation_recovery or preserved_active_unavailable_worktree_root_classifies_as_ambiguous or preserved_active_worktree_path_propagates_internal_attribute_error'`
  - Before implementation: failed on the two new regressions.
  - After implementation: passed, `3 passed, 265 deselected`.

- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.

- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Additional Check

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery -q`
  - Failed on existing broader cases outside this patch:
    `test_stale_active_scan_closed_connection_does_not_terminal_fail_workspace`
    and `test_preserved_active_validation_salvage_without_executor_blocks_stale_cleanup`.
  - Each failure reproduced when run individually. The first concerns
    running-status runtime inspection after a simulated DB closed connection;
    the second times out in existing no-executor salvage coverage. Neither
    depends on the new `pushing` guard or the worktree-path `AttributeError`
    change.
