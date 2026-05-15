# Protected Scope Remote Restore Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6CS4V4` reports that protected-scope dirty path
restoration is verified against the merge base with the PR branch instead of the
fetched remote PR branch tree. If the remote PR branch advanced on a protected
path, a worktree that correctly restored that path to the current remote tree can
still be blocked.

## Requirements Checklist

- Verify protected dirty tracked paths against the fetched remote PR branch tree.
- Preserve fail-closed behavior when fetch or diff verification fails.
- Keep untracked protected paths blocked.
- Add or update focused regression coverage for the remote-advanced case.
- Keep the change scoped to PR monitor protected-scope restore verification.

## Implementation Steps

1. Add a unit regression for a protected tracked path that differs from the merge
   base but matches `FETCH_HEAD`.
2. Change `_protected_scope_violations_not_restored_to_remote_branch` to use
   `git diff --quiet FETCH_HEAD -- <path>` for tracked restore verification.
3. Update existing test expectations that asserted the old merge-base restore
   comparison.
4. Run the narrow affected unit tests, then broader lint/type/test checks as
   practical.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
