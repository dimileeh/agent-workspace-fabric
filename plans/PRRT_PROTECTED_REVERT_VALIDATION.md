# PRRT Protected Revert Validation

Plan reference: `plans/PRRT_PROTECTED_REVERT_PLAN.md`

## Requirement Status

- Preserve the default pre-commit protected dirty-path block for ordinary comment, CI, and monitor repairs: Complete.
  - Existing `_commit_dirty_worktree` callers keep protected repair enabled by default; the new baseline verification is only used when `protected_scope_revert_remote_branch` is passed.
- Allow the committed protected-scope repair path to commit protected file changes only when the protected paths no longer appear in the unpushed committed diff after the repair: Complete.
  - `_repair_protected_scope_commits_before_push` now passes the PR remote branch to `_commit_dirty_worktree`; protected dirty paths are allowed only when `git diff --quiet <merge-base> -- <path>` proves they match the remote PR branch baseline, and the existing post-commit `_protected_scope_push_block` still gates the push.
- Fail closed when AWF cannot verify the committed diff after repair: Complete.
  - Verification errors return `None` before add/commit; regression coverage confirms no commit is attempted.
- Add a regression test that fails before the fix and proves the protected-file revert can be committed and pushed: Complete.
  - `test_ci_fix_commits_verified_protected_revert_during_scope_repair` failed before implementation because the monitor invoked a second protected-scope repair prompt; it now passes.
- Run the narrow runtime unit test coverage for the changed behavior: Complete.
  - Commands and results below.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `plans/PRRT_PROTECTED_REVERT_PLAN.md`
- `plans/PRRT_PROTECTED_REVERT_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "test_ci_fix_commits_verified_protected_revert_during_scope_repair"`: passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "protected_scope or ci_fix or protected_revert"`: passed, 18 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`: passed.

No remaining gaps.
