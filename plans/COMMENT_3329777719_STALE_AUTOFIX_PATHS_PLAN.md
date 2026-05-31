# Comment 3329777719 Stale Autofix Paths Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6zED` reports that monitor dirty-worktree
commit retries pass the pre-protected-repair dirty path snapshot into the
pre-commit autofix retry helper. If protected-scope repair changes the dirty set
before the commit attempt, deterministic hook edits to the repaired dirty set can
be rejected as unsafe and left dirty.

Scope is limited to the PR monitor dirty commit path and a focused regression
test. Broad AWF/GitHub validation remains owned by AWF after agent completion.

## Requirements Checklist

- Add a regression test proving protected-scope repair refreshes the dirty-path
  safety boundary used by deterministic pre-commit autofix retry.
- Keep the retry helper's subset safety intact for unrelated dirty paths.
- Update implementation to pass the post-repair dirty paths to the retry helper.
- Run only focused tests/checks for the changed runtime behavior.

## Implementation Steps

1. Add a focused unit test around `_commit_dirty_worktree` where protected-scope
   repair returns a different dirty status than the initial snapshot and a
   deterministic pre-commit hook modifies the repaired path.
2. Confirm the test fails against the current implementation when practical.
3. Refresh `changed_paths` after protected-scope repair returns fresh status.
4. Re-run the focused regression test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k refreshed`

Pass criteria: the focused regression test passes, and full AWF/GitHub
validation is left to AWF after agent completion.
