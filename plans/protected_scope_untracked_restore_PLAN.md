# Protected Scope Untracked Restore Plan

## Problem Statement And Scope

PR monitor protected-scope repair currently treats every untracked protected path
as a remaining violation. That blocks the safe repair path where a committed
branch diff deleted a protected file, the repair agent restores that file from
the remote PR branch, and `git status --porcelain` reports the restored file as
untracked because `HEAD` no longer tracks it.

Scope is limited to `src/awf/runtime/pr_monitor_runner.py` and focused unit
coverage for the protected-scope restore filter.

## Requirements Checklist

- Add a regression test proving an untracked protected file is allowed when its
  blob matches the remote PR branch tree.
- Keep untracked protected files as violations when they cannot be verified as
  matching the remote PR branch tree.
- Preserve the existing tracked-file restore verification behavior.
- Fail closed when the remote PR branch baseline cannot be fetched.
- Do not push or switch branches; commit the local fix on the current branch.

## Implementation Steps

1. Add a failing unit test beside the existing tracked restore test.
2. Update the restore-filter helper to verify untracked protected paths against
   `FETCH_HEAD` instead of automatically leaving them in `remaining`.
3. Run the focused test to confirm the regression is fixed.
4. Run relevant static checks and the touched unit-test file as practical.
5. Record validation evidence in `plans/protected_scope_untracked_restore_VALIDATION.md`.
6. Stage only changed files and commit with the PR thread id.

## Assumptions/Changes

- Broader verification exposed stale fake command queues in existing
  committed-repair tests that did not account for the current `rev-parse HEAD`
  history-rewrite checks. Adjust those test setups only enough for the queued
  Git results to line up with the implementation they are already testing.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: all commands complete successfully, or any unrelated environment
failure is documented in the validation artifact.
