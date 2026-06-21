# PRRT_kwDOSJAM6s6K8DPt Plan

## Problem Statement and Scope

The unresolved PR review thread reports that `remote_repair._repair_operation_start_head_result`
validates the current worktree `HEAD` when `mirror_path_for_worktree()` returns `None`, even when
the operation will return a fallback SHA. This can accept a stale or dangling fallback SHA as the
repair operation baseline.

Scope is limited to validating fallback operation-start heads in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and adding focused regression coverage for
that behavior.

## Requirements Checklist

- [ ] Verify the review claim against the current implementation.
- [ ] Add a focused failing regression for a no-mirror fallback SHA that is not a valid commit.
- [ ] Validate the fallback SHA itself with `cat-file -e <fallback>^{commit}` when no mirror path is
      available.
- [ ] Preserve existing mirror-based fallback validation behavior.
- [ ] Run targeted tests only; AWF/GitHub own broad validation after this agent exits.

## Implementation Steps

1. Read the targeted code and nearby tests.
2. Add a unit test covering the no-mirror, dangling-fallback branch.
3. Add a worktree-scoped commit-object existence helper and use it for no-mirror fallback
   validation.
4. Run the focused regression test and the surrounding focused test file if practical.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  - Passes with the new regression included.
- Full AWF/GitHub validation is intentionally not run in the agent phase per the workspace
  contract.
