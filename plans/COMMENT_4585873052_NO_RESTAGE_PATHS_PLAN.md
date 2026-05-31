# COMMENT 4585873052 No-Restage Paths Plan

## Problem Statement and Scope

Review comment `issue:4585873052` reports that the
`if not restage_paths:` early exit in
`_retry_monitor_precommit_autofix_commit_once` lacks explicit test coverage.
The branch is reachable when `git status --porcelain` reports only staged dirty
paths, such as `M  path.py`, after a failed pre-commit autofix commit.

Scope is limited to focused unit coverage for that early exit and the required
plan/validation artifacts. No source behavior change is planned.

## Requirements Checklist

- Add a focused regression test where dirty paths exist but no worktree-modified
  repair paths are available to restage.
- Assert the retry returns `None` and does not invoke `git add` or a retry
  commit.
- Keep changes scoped to PR monitor commit autofix tests and plan artifacts.
- Run targeted local validation only; broad AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add an async unit test in
   `tests/unit/runtime/test_pr_monitor_commit_autofix.py` using staged-only
   porcelain output.
2. Run the focused new test.
3. Run the focused commit-autofix unit test file.
4. Record results in
   `plans/COMMENT_4585873052_NO_RESTAGE_PATHS_VALIDATION.md`.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_returns_none_when_only_staged_repair_paths_remain -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Both focused pytest commands must pass. Do not run full coverage,
whole-repository unit suites, or CI-equivalent validation in this workspace
phase.
