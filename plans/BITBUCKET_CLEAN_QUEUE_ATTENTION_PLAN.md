# Bitbucket CLEAN Queue Attention Plan

## Problem Statement And Scope

The PR monitor queue/reviewer/grace wait helper treats `MergeStateStatus.CLEAN` as
a resolved branch-protection block. That is valid for GitHub, where `CLEAN`
reflects the forge merge-state signal, but Bitbucket open PRs map to `CLEAN`
because Bitbucket Cloud has no equivalent pre-merge signal. For Bitbucket, a
`CLEAN` status must not clear an existing merge-block attention marker while the
monitor is parked in queue/reviewer/grace waits.

Scope is limited to the merge-block attention queue verdict and its direct
callers/tests.

## Requirements Checklist

- Add a focused regression proving Bitbucket `CLEAN` preserves the marker and
  `awaiting_human_since` during a merge queue wait.
- Preserve existing GitHub behavior: GitHub `CLEAN` still clears resolved merge
  block attention.
- Keep the change scoped to `src/awf/runtime/pr_monitor_runner/**`, `tests/**`,
  and this plan/validation artifact.
- Run only focused tests for the changed behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Update the queue-verdict helper to accept forge context from the existing
   `RepoRef` passed through merge/gate callers.
2. Classify Bitbucket `CLEAN` as `indeterminate` so wait paths preserve existing
   attention unless a stronger signal is available.
3. Thread `repo` into direct queue-attention helper calls and status recheck
   decisions.
4. Add a Bitbucket queue-wait regression next to the existing merge queue
   attention tests.

## Verification Commands And Pass Criteria

- Run the targeted test(s) in `tests/unit/runtime/test_merge_queue_ordering.py`
  covering GitHub resolved and Bitbucket clean-preserve behavior.
- Pass criteria: the focused tests pass locally; full AWF/GitHub validation is
  left to the post-agent validation pipeline.
