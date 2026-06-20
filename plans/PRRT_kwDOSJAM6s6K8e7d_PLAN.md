# PRRT_kwDOSJAM6s6K8e7d Plan

## Problem Statement And Scope

The PR review reports that no-mirror repair start-head fallback validation checks the worktree `HEAD` object instead of validating the supplied fallback head SHA. Scope is limited to the fallback validation in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and the focused regression that covers this path.

## Requirements Checklist

- Confirm the current no-mirror fallback path accepts a fallback SHA based on `HEAD` object availability.
- Validate the supplied fallback SHA as a commit when no mirror is available.
- Preserve existing mirror fallback validation behavior.
- Run focused tests for the changed behavior only; full AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Run the targeted regression for dangling no-mirror fallback validation.
2. Update no-mirror fallback validation to call the existing worktree commit-object helper with `fallback_head`.
3. Re-run the targeted regression.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k dangling_no_mirror_fallback`

Pass criteria: the focused regression passes and no broad validation suite is run in the agent phase.
