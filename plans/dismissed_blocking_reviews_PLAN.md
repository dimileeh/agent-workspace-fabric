# Dismissed Blocking Reviews Plan

## Problem Statement And Scope

AWF's effective blocking review calculation currently ignores `DISMISSED`
review nodes while selecting each reviewer's latest effective review. A later
dismissed review can leave an earlier `CHANGES_REQUESTED` review in
`blocking_reviews`, causing AWF to notify a human even when GitHub no longer
treats the dismissed review as merge-blocking.

Scope is limited to the effective blocking review parser in
`src/awf/common/github_client.py`, focused unit coverage in
`tests/unit/common/test_github_client.py`, and the required plan/validation
artifacts.

## Requirements Checklist

- Preserve review-level comments in `unresolved_review_comments`.
- Keep `COMMENTED` reviews from clearing an earlier `CHANGES_REQUESTED` review.
- Treat a later `DISMISSED` review from the same reviewer as clearing an
  earlier `CHANGES_REQUESTED` review for `blocking_reviews`.
- Preserve `APPROVED` clearing behavior and non-counting/viewer-owned review
  exclusions.
- Stage and commit only the files changed for this thread.

## Implementation Steps

1. Update the existing dismissed-review regression to assert that a later
   `DISMISSED` review clears the earlier blocking review.
2. Run that focused test and confirm it fails before changing production code.
3. Update `_effective_blocking_reviews` so `DISMISSED` participates in latest
   effective state selection but is not emitted as a blocker.
4. Run the focused regression and the relevant GitHub client unit tests.
5. Record validation evidence in
   `plans/dismissed_blocking_reviews_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_dismissed_review_clears_blocking_review -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Pass criteria: the focused regression fails before the production fix, all
verification commands pass after the fix, and the validation artifact marks
every requirement complete or documents any remaining gap.
