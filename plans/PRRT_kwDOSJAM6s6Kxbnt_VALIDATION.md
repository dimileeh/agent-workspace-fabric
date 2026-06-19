# PRRT_kwDOSJAM6s6Kxbnt Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Kxbnt_PLAN.md`

## Requirement Status

- Add a focused regression test showing snapshot mode issues one batched
  `git check-ignore --stdin -z` call for multiple empty directory candidates:
  Complete. Added
  `test_snapshot_empty_untracked_dirs_batch_check_ignore_candidates`.
- Update `_snapshot_empty_untracked_dirs` to collect empty directory candidates
  and batch ignore checks through `_ignored_paths`: Complete. Snapshot mode now
  collects candidates before a single batched ignore probe.
- Preserve existing behavior for non-ignored empty directories, ignored empty
  directories, and failure propagation from `git check-ignore`: Complete.
  Focused adjacent tests pass.
- Run targeted tests for the changed validation worktree behavior only:
  Complete. Focused pytest and ruff checks passed.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/PRRT_kwDOSJAM6s6Kxbnt_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Kxbnt_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_snapshot_empty_untracked_dirs_batch_check_ignore_candidates -q`
  - Failed before implementation with five `check-ignore` calls.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_ignores_wildcard_ignored_empty_dir_when_opted_in tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_fails_when_check_ignore_fails -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_snapshot_empty_untracked_dirs_preserves_tracked_deinitialized_submodule tests/unit/runtime/test_validation_worktree.py::test_snapshot_empty_untracked_dirs_treats_nested_git_marker_as_boundary tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_reports_wildcard_ignored_empty_dir_by_default -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_wildcard_ignored.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase per workspace
contract; AWF owns broad validation, provenance, and merge gating after agent
completion.

## Gaps

None.
