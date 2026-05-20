# PRRT_kwDOSJAM6s6Dckhq Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Dckhq_PLAN.md`

## Requirement Status

- Complete: Preserve `remote_push_branch` for active-execution salvage
  replacements whose task kinds depend on an external monitor/sync remote
  branch.
- Complete: Keep ordinary `feature_branch_pr` replacement behavior unchanged;
  the existing regression still asserts `replacement.remote_push_branch is None`.
- Complete: Added a regression test that failed before the implementation
  change with `replacement.remote_push_branch == None`.
- Complete: Ran the focused replacement tests and ruff on touched files.

## Evidence

- Changed `src/awf/control/worker.py` to pass
  `_preserved_active_replacement_remote_push_branch(ws)` into
  `WorkspaceRepository.create_replacement_from`.
- Added
  `test_preserved_active_without_usable_work_preserves_sync_remote_push_branch`
  in `tests/unit/control/test_worker.py`.
- Pre-fix failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work_preserves_sync_remote_push_branch'`
  failed because the replacement remote branch was `None`.
- Post-fix pass:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work'`
  passed with `2 passed, 200 deselected`.
- Lint pass:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
