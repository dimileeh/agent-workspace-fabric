# PRRT_kwDOSJAM6s6F68aG Commit Autofix Untracked Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F68aG` reports that the PR monitor
pre-commit autofix retry treats `git status --porcelain` `??` entries as
worktree-modified paths. The retry then requires those untracked paths to appear
in deterministic hook repair output, so a normal monitor repair with an
untracked operation path plus a tracked hook-fixed file can skip the safe
restage and leave hook edits dirty for the next pass.

Scope is limited to the PR monitor commit autofix helper and focused unit
coverage for this retry behavior.

## Requirements Checklist

- Add a regression test showing untracked operation paths do not block retrying
  a deterministic hook repair on a tracked file.
- Preserve the existing operation-scope guard so untracked paths outside the
  monitor operation still block the retry.
- Preserve the existing safety rule that unrelated tracked worktree-modified
  paths outside the hook repair set block the retry.
- Restage only hook-modified repair paths, not unrelated untracked paths.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add a targeted async unit test in
   `tests/unit/runtime/test_pr_monitor_commit_autofix.py` with porcelain output
   containing `?? new.py` and `MM fixed.py`.
2. Confirm the regression fails against the current implementation when
   practical.
3. Update `_worktree_modified_paths_from_porcelain` so it excludes `??`
   untracked entries from the worktree-modified repair-path match.
4. Run the focused regression and the touched unit test file.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_untracked_operation_paths -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Pass criteria: focused tests and touched-file lint pass. Full AWF/GitHub
validation remains owned by AWF after agent completion.
