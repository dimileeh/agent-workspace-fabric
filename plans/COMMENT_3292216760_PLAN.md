# Comment 3292216760 Plan

## Problem Statement And Scope

Fix the quiet-period fallback logic used by `fetch_pr_status` so that PR fallback anchoring
uses a push/update signal from the PR itself when there is no external reviewer activity.
The scope is limited to `src/awf/common/github_client.py` and its unit tests.

## Requirements Checklist

- Add the PR update timestamp (`pullRequest.updatedAt`) to the `_GQL_PR_STATE` selection.
- Pass PR `updatedAt` into `_quiet_period_anchor` and make fallback choose the newest
  timestamp among `createdAt`, `updatedAt`, and head commit `committedDate`.
- Preserve existing review-activity precedence behavior.
- Update unit tests to assert the new fallback behavior with a stale head commit but newer PR `updatedAt`.
- Keep changes scoped and commit with a conventional message tied to this review thread.

## Implementation Steps

1. Read `_GQL_PR_STATE`, the `fetch_pr_status` fallback call site, and `_quiet_period_anchor`.
2. Update the GraphQL query string to include `updatedAt` on `pullRequest`.
3. Update `fetch_pr_status` to pass parsed `updatedAt` to `_quiet_period_anchor`.
4. Update `_quiet_period_anchor` to accept `pr_updated_at` and include it in fallback candidates.
5. Update/add unit tests in `tests/unit/common/test_github_client.py` for the stale-commit/new-PR-update path.
6. Run a focused pytest for the touched test module.
7. Create `plans/COMMENT_3292216760_VALIDATION.md` with evidence and requirement status.
