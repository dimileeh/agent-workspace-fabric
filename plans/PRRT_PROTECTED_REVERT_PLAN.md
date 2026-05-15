# PRRT Protected Revert Plan

## Problem Statement and Scope

PR monitor committed-diff repair can ask the agent to remove committed protected-scope edits. A normal safe repair may leave a protected file dirty because the agent restored it to the remote PR branch/base content, but `_commit_dirty_worktree` currently re-runs the pre-commit protected dirty-path repair and can block that revert before it is committed.

Scope is limited to the committed protected-scope repair path in `src/awf/runtime/pr_monitor_runner.py` and focused unit coverage.

## Requirements Checklist

- Preserve the default pre-commit protected dirty-path block for ordinary comment, CI, and monitor repairs.
- Allow the committed protected-scope repair path to commit protected file changes only when the protected paths no longer appear in the unpushed committed diff after the repair.
- Fail closed when AWF cannot verify the committed diff after repair.
- Add a regression test that fails before the fix and proves the protected-file revert can be committed and pushed.
- Run the narrow runtime unit test coverage for the changed behavior.

## Implementation Steps

1. Add a focused unit test for `_run_ci_fix` where the first committed diff contains a protected workflow file, the repair leaves that workflow file dirty as a revert plus a source change, and the post-repair committed diff is clean of protected paths.
2. Thread an explicit committed-repair option through `_commit_dirty_worktree` so normal callers keep existing behavior.
3. In the committed repair path, bypass the dirty protected-path repair only after a protected committed-diff recheck succeeds and shows no remaining protected violations.
4. Validate with the new test and nearby protected-scope tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "protected_scope or ci_fix"`
  - Passes without failures.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Passes without lint errors.
