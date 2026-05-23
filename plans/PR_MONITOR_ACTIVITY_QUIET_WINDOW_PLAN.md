# PR Monitor Activity Quiet Window Plan

## Problem

AWF currently starts `NON_CHECK_REVIEWER_SETTLE` when the PR first becomes
otherwise merge-ready. On repositories with long CI, that adds an unnecessary
15 minute wait after reviewers already had the full CI duration to comment.

## Requirements

- Anchor non-check reviewer quiet time to latest relevant external PR review
  activity, using `updatedAt` where GitHub exposes it.
- Count review-thread comments, reviews, and top-level PR comments that AWF
  treats as review feedback; exclude viewer/AWF-authored status noise.
- If no relevant review activity exists, use PR/head activity as the quiet
  anchor so fast CI still gives async reviewers time.
- Keep existing profile keys and `NON_CHECK_REVIEWER_SETTLE` reason code.
- Record operator-visible payload fields for activity anchor, quiet-until, and
  remaining wait.
- Leave the separate `pre_merge_settle_seconds` race guard unchanged.

## Implementation Steps

1. Extend PR monitor dataclasses with updated activity timestamps and quiet
   anchor metadata.
2. Extend GitHub GraphQL parsing to collect external review activity before
   unresolved-only filtering, including resolved review-thread comments.
3. Replace the non-check reviewer settle helper with activity-clock behavior
   when a quiet anchor is available, while preserving legacy behavior for
   statuses without anchor metadata.
4. Apply the same quiet clock before manual ready notifications for
   `auto_merge=false` workspaces.
5. Add focused unit coverage for parsing, helper decisions, runner waits,
   manual handoff, and operation payloads.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q -k 'review_activity or updated_at or resolved_thread'`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`
- `uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py`
