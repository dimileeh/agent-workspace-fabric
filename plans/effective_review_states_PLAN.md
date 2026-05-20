# Effective Review States Plan

## Problem Statement And Scope

AWF's GitHub PR status parser currently chooses the newest review from each
reviewer before checking whether that review blocks merge. GitHub's effective
review state ignores advisory `COMMENTED` reviews and dismissed reviews for
merge gating, so a later comment must not clear an earlier active
`CHANGES_REQUESTED` review.

Scope is limited to effective blocking review calculation in
`src/awf/common/github_client.py` and focused unit coverage in
`tests/unit/common/test_github_client.py`.

## Requirements Checklist

- Preserve advisory review comments in `unresolved_review_comments`.
- Keep `CHANGES_REQUESTED` reviews blocking until the same reviewer submits a
  later `APPROVED` review.
- Ensure later `COMMENTED` reviews from the same reviewer do not clear an
  earlier blocking review.
- Ignore non-merge-gating review states such as `DISMISSED` while calculating
  effective blocking reviews.
- Keep existing bot advisory and empty-body review behavior unchanged.

## Implementation Steps

1. Add a failing regression test for a `CHANGES_REQUESTED` review followed by a
   later `COMMENTED` review from the same reviewer.
2. Add focused coverage for a dismissed latest review if needed to prove
   non-merge-gating states are skipped.
3. Update `_effective_blocking_reviews` to consider only `APPROVED` and
   `CHANGES_REQUESTED` review states when selecting the latest effective review
   per reviewer.
4. Run the focused regression test, then the relevant GitHub client test module
   and lint for the touched files.
5. Record validation evidence in `plans/effective_review_states_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_commented_review_does_not_clear_blocking_review -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Pass criteria: all commands complete successfully, and the validation artifact
marks every requirement complete or documents any remaining gap.
