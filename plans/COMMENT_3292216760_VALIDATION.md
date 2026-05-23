# Comment 3292216760 Validation

Plan reference: `plans/COMMENT_3292216760_PLAN.md`

## Requirement Status

- Complete: Added PR update timestamp (`pullRequest.updatedAt`) to the GraphQL selection.
- Complete: Passed PR `updatedAt` through `fetch_pr_status` into `_quiet_period_anchor`.
- Complete: Updated quiet-anchor fallback to choose the newest timestamp across `createdAt`,
  `updatedAt`, and head commit `committedDate`.
- Complete: Preserved existing precedence of explicit external reviewer activity over fallback
  anchors.
- Complete: Added focused regression coverage for fallback behavior using a stale head commit and
  newer PR `updatedAt`.
- Complete: Kept edits scoped to the GitHub client and its unit tests.

## Evidence

- Changed `src/awf/common/github_client.py`:
  - Added `updatedAt` to `_GQL_PR_STATE`.
  - Added `pr_updated_at` argument to `_quiet_period_anchor` call site.
  - Added `pr_updated_at` into fallback comparison order.
- Changed `tests/unit/common/test_github_client.py`:
  - Updated expectations in
    `test_viewer_authored_feedback_does_not_reset_quiet_anchor` to ensure PR updates become
    the fallback anchor when newer than commit data.
  - Added `test_stale_head_commit_uses_pr_updated_at_as_quiet_anchor` to lock this behavior.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -k "quiet" -q`
  Result: passed (`3 passed, 143 deselected`).

## Gaps

None.
