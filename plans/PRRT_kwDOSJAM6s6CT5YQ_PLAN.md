# PRRT_kwDOSJAM6s6CT5YQ Plan

## Problem Statement and Scope

The protected-scope committed-repair push path treats
`_commit_dirty_worktree()` returning `False` as a harmless no-op. That return
value is ambiguous: it can mean there was nothing to commit, or it can mean git
status/add/commit or protected-scope cleanup failed. If repair edits remain
dirty, the monitor can continue to the committed-diff push check and publish an
unchanged HEAD while dropping the repair work.

Scope is limited to `src/awf/runtime/pr_monitor_runner.py`, the focused unit
coverage for this edge, and this plan/validation record.

## Requirements Checklist

- Add a regression test for `_repair_protected_scope_commits_before_push()` when
  `_commit_dirty_worktree()` returns `False` and a subsequent worktree status is
  still dirty.
- Preserve the existing clean/no-commit case where a clean status after the
  falsy return may continue to the protected-scope push recheck.
- Fail closed before `_protected_scope_push_block()` and `_git_push_result()`
  when the post-commit status check fails or reports remaining dirty changes.
- Return a failed `_GitPushResult` using the protected-scope repair failure
  reason so the monitor records the failure as repair-specific.
- Keep changes minimal and aligned with existing logging/result patterns.

## Implementation Steps

1. Add a focused failing unit test near the existing protected-scope committed
   repair tests.
2. Run that test and confirm it fails against current behavior.
3. Update `_repair_protected_scope_commits_before_push()` to re-check
   `git status --porcelain` after a falsy `_commit_dirty_worktree()` result.
4. If the re-check fails or is dirty, log and return a failed push result before
   the committed-diff recheck/push.
5. Re-run the focused tests and then the narrow runtime test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_fails_when_commit_returns_false_with_dirty_worktree -q`
  - First run should fail before implementation.
  - Final run should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_fails_when_commit_returns_false_with_dirty_worktree -q`
  - Both the existing no-op path and new dirty failure path pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - The focused runtime coverage file passes.
