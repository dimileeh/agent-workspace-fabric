# CI Active Salvage PR Recovery Plan

## Problem Statement And Scope

PR #272 fails the Python full coverage check in preserved active-execution
recovery tests. The failing paths are limited to control-worker recovery after a
worker restart:

- a preserved active `pushing` workspace whose open PR must be recovered by
  branch name when `remote_push_branch` is missing;
- an adopted `sync_feature_pr` workspace whose open PR lives on a fork head repo.

The fix must keep the CI checks intact, preserve AWF branch/push ownership, and
avoid broad worker refactors.

## Requirements Checklist

- Reproduce the two reported pytest failures locally before coding.
- Preserve exactly one PR-monitor handoff for a recovered active execution in
  the focused branch-lookup fallback path.
- Accept the adoption policy's `task_policy.pr_adoption.head_repo_slug` as the
  expected PR head repository when resolving open PRs for adopted sync workspaces.
- Keep non-adopted branch PR recovery strict about head-repo mismatches.
- Add or update regression coverage without weakening the failing assertions.
- Validate with the focused pytest command, then run narrow lint/type/test
  commands appropriate for the touched Python control-plane code.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Inspect the failing tests and worker recovery code for branch lookup, salvage
   monitor attachment, and monitor dispatch idempotency.
2. Teach preserved-active branch PR lookup to use an optional expected head repo
   slug, sourced from PR adoption policy when present.
3. Add a scoped guard so a PR monitor resumed as part of active-execution salvage
   is not immediately resumed again by the same worker after the first handoff.
4. Re-run the focused failing pytest nodes and adjust only if the behavior still
   diverges from the PRD-backed recovery contract.
5. Run narrow validation for `src/awf/control/worker.py` and
   `tests/unit/control/test_worker.py`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_lookup_falls_back_to_branch_name tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_adopted_sync_feature_pr_fork_head_repo_attaches_monitor -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  must pass, or any broader project mypy limitation must be documented.
