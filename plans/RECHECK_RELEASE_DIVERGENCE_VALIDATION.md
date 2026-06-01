# Recheck Release Divergence Validation

Plan reference: `plans/RECHECK_RELEASE_DIVERGENCE_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `sync_release_pr` rechecks
  divergence after setup and completes as a no-op when the second count is zero.
- Complete: The regression asserts no `gh pr list` or `gh pr create` call occurs
  after the post-setup count reports no commits ahead.
- Complete: Existing setup failure ordering is preserved; setup still happens
  before PR lookup/create when the initial count is positive.
- Complete: Validation stayed focused. Full AWF/GitHub validation is managed by
  AWF after agent completion and was not executed here.
- Complete: Local commit will be created after final status review.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py`
- `plans/RECHECK_RELEASE_DIVERGENCE_PLAN.md`
- `plans/RECHECK_RELEASE_DIVERGENCE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff::test_rechecks_commits_ahead_after_setup_and_completes_no_op_when_source_catches_up -q`
  - Failed before implementation: current code called `gh pr list` instead of a
    post-setup `git fetch` / `git rev-list`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff::test_rechecks_commits_ahead_after_setup_and_completes_no_op_when_source_catches_up -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff -q`
  - Passed: 12 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py`
  - Passed.

## Gaps

No planned requirements remain missing or partial.
