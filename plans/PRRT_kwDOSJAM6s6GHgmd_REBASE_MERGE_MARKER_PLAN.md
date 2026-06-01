# PRRT_kwDOSJAM6s6GHgmd Rebase Merge Marker Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GHgmd` reports that a rebase-only merge can complete through `gh pr merge --rebase` while `GitHubClient.merge_pr` returns an empty merge SHA because GitHub does not create a merge commit for rebase merges. The monitor then completes the workspace with an empty `pr_merge_sha`, so completed-PR cleanup predicates still classify the workspace as an unmerged PR.

Scope is limited to the PR monitor merge-method loop and focused regression coverage for rebase merge completion markers.

## Requirements Checklist

- Add a regression test that exercises a rebase-only branch where the merge call succeeds but returns an empty SHA.
- Ensure successful rebase merges complete with a non-empty `pr_merge_sha` marker.
- Preserve existing merge SHA behavior for squash and merge methods when GitHub returns a merge commit SHA.
- Keep validation focused; AWF/GitHub owns broad validation after agent completion.

## Implementation Steps

1. Add a failing unit test in the merge-method monitor tests for a rebase-only merge returning `""`.
2. Update the merge loop to convert an empty rebase merge SHA into a stable non-empty completion marker before leaving the critical section.
3. Run the targeted test first to confirm failure, then run focused tests for the touched merge-method behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`

Pass criteria: the new regression and existing merge-method unit tests pass. Full AWF/GitHub validation is intentionally left to AWF after agent completion per workspace contract.
