# Protected Scope Untracked Restore Validation

Plan reference: `plans/protected_scope_untracked_restore_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving an untracked protected file is allowed
  when its blob matches the remote PR branch tree.
- Complete: Keep untracked protected files as violations when they cannot be
  verified as matching the remote PR branch tree.
- Complete: Preserve the existing tracked-file restore verification behavior.
- Complete: Fail closed when the remote PR branch baseline cannot be fetched.
- Complete: Do not push or switch branches; all work stayed on the current AWF
  branch.

## Evidence

- Added unit coverage in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` for matching and
  mismatched untracked protected restores.
- Updated `src/awf/runtime/pr_monitor_runner.py` so untracked protected paths are
  compared to the fetched remote PR branch by Git blob id instead of being kept
  as automatic remaining violations.
- Confirmed TDD failure before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "untracked_restore"`
  failed with 2 failures.
- Confirmed focused pass after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "untracked_restore"`
  passed with 2 tests.
- Confirmed adjacent repair coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "untracked_restore or blocks_committed_protected_quality_gate_edits_after_retry or commit_repair_fails_when_commit_returns_false_with_dirty_worktree or commits_verified_protected_revert or stops_when_protected_revert_diff_baseline_unavailable"`
  passed with 6 tests.
- Confirmed full touched unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  passed with 137 tests.
- Confirmed static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  passed.
- Confirmed formatting:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  passed.
- Confirmed type checks:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

No remaining gaps.
