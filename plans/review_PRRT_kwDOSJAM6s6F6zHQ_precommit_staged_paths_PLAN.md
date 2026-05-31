# Review PRRT_kwDOSJAM6s6F6zHQ Pre-Commit Staged Paths Plan

## Problem Statement and Scope

The PR monitor pre-commit autofix retry currently compares every path reported by
`git status --porcelain` after a failed commit against the hook-reported repair
paths. Git still reports already staged files that were not modified by the hook,
so a normal hook autofix on one file can block retrying a multi-file monitor
repair commit.

Scope is limited to the PR monitor autofix retry helper and focused regression
coverage for this review thread.

## Requirements Checklist

- Add a regression test showing an unaffected staged operation path does not
  block a deterministic pre-commit autofix retry.
- Keep rejecting retry attempts when a worktree-modified path is outside the
  hook repair set.
- Keep retry scope bounded to paths that were dirty at the start of the monitor
  commit operation.
- Restage only the hook-modified repair paths needed for the retry.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add a focused async unit test for `_retry_monitor_precommit_autofix_commit_once`
   with porcelain output containing `M  other.py` and `MM fixed.py`.
2. Confirm the regression fails against the current implementation.
3. Update the retry helper to separate all dirty operation paths from paths with
   worktree-side modifications.
4. Restage the safe worktree-modified repair subset and keep staged-only
   operation paths allowed.
5. Run the focused regression test file and record evidence in validation.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Pass criteria: the focused test file passes. Full AWF/GitHub validation remains
owned by AWF after agent completion.
