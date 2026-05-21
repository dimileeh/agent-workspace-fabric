# CI Active Salvage PR Recovery Validation

Plan reference: `plans/CI_ACTIVE_SALVAGE_PR_RECOVERY_PLAN.md`

## Requirement Status

- Reproduce the two reported pytest failures locally before coding: Complete.
  The AWF-provided focused command failed with both reported assertions before
  implementation.
- Preserve exactly one PR-monitor handoff for a recovered active execution in
  the focused branch-lookup fallback path: Complete.
  `src/awf/control/worker.py` now marks monitor recovery operations that come
  from active-execution salvage and suppresses immediate duplicate handoffs for
  the worker's normal monitor-claim lease interval.
- Accept the adoption policy's `task_policy.pr_adoption.head_repo_slug` as the
  expected PR head repository when resolving open PRs for adopted sync
  workspaces: Complete.
  Preserved branch PR lookup now accepts an explicit expected head repo slug and
  the recovery path sources it from PR adoption policy.
- Keep non-adopted branch PR recovery strict about head-repo mismatches:
  Complete. The resolver still defaults to the base repository slug when no
  adoption head repo is specified.
- Add or update regression coverage without weakening the failing assertions:
  Complete. The existing failing regression tests remain unchanged and now pass.
- Validate with focused and narrow Python checks: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `plans/CI_ACTIVE_SALVAGE_PR_RECOVERY_PLAN.md`
- `plans/CI_ACTIVE_SALVAGE_PR_RECOVERY_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_lookup_falls_back_to_branch_name tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_adopted_sync_feature_pr_fork_head_repo_attaches_monitor -q`
  - Before fix: failed with the two CI assertions.
  - After fix: `2 passed in 3.77s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery -q`
  - `118 passed in 104.72s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - `Success: no issues found in 1 source file`.

## Gaps

None.
