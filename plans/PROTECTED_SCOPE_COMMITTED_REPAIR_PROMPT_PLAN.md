# Protected Scope Committed Repair Prompt Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6CSSJl` reports that the pre-push protected-scope repair flow handles edits already committed on the PR branch, but reuses a prompt that tells the repair agent to remove edits from the worktree. That can leave the committed protected-scope diff intact and cause the second protected-scope check to fail.

## Scope

Keep the existing worktree repair prompt for dirty/uncommitted changes. Add a committed-diff-specific prompt for `_repair_protected_scope_commits_before_push` so agents are told the violating edits are already committed locally and must be removed from branch history relative to the PR head.

## Requirements Checklist

- Add a regression test proving the committed-diff repair call receives history-level guidance.
- Preserve the existing dirty-worktree protected-scope repair behavior and wording.
- Route only the committed pre-push protected-scope repair path to the committed-diff prompt variant.
- Keep changes scoped to monitor-runner behavior and tests.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` that calls `_repair_protected_scope_commits_before_push` and asserts the adapter prompt says the edits are already committed and must be removed from branch history.
2. Add a dedicated helper method for committed protected-scope repair prompts, reusing common path/owned-path rendering where practical.
3. Update `_repair_protected_scope_commits_before_push` to call the committed prompt helper.
4. Run focused tests for the affected monitor-runner coverage file.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
```

Pass criteria: the focused test file passes, including the new regression.
