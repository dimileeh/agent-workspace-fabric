# Reviewer Authority Blocking Reviews Plan

## Problem Statement And Scope

AWF currently treats the latest effective `CHANGES_REQUESTED` review from every
reviewer as merge-blocking. GitHub required-review blocking only treats change
requests from reviewers with repository push/write-level authority as merge
blockers, so advisory change requests from non-counting reviewers can trigger
unnecessary PR monitor escalation.

Scope is limited to GitHub PR status review parsing in
`src/awf/common/github_client.py` and focused unit coverage in
`tests/unit/common/test_github_client.py`.

## Requirements Checklist

- Fetch reviewer authority for pull request reviews.
- Preserve non-counting review bodies in `unresolved_review_comments`.
- Do not include explicit non-counting `CHANGES_REQUESTED` reviews in
  `blocking_reviews`.
- Preserve existing effective-review behavior for counting reviewers, including
  later approvals clearing earlier change requests.
- Prefer conservative behavior when authority data is absent from older/fake
  payloads.

## Implementation Steps

1. Add a failing regression test for an explicit non-counting
   `CHANGES_REQUESTED` review.
2. Update the GraphQL review selections to fetch reviewer push authority.
3. Carry reviewer authority through fetched review parsing.
4. Filter non-counting reviews from effective blocking-review detection while
   leaving advisory review comments visible.
5. Run focused and module-level tests plus lint for touched files.
6. Record validation evidence in
   `plans/reviewer_authority_blocking_reviews_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_non_counting_changes_requested_review_is_advisory_not_blocking -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Pass criteria: all commands complete successfully, and the validation artifact
marks every requirement complete or documents a defer reason.
