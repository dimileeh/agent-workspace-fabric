# Dismissed Blocking Reviews Validation

Plan reference: `plans/dismissed_blocking_reviews_PLAN.md`

## Requirement Status

- Complete: Preserve review-level comments in `unresolved_review_comments`.
- Complete: Keep `COMMENTED` reviews from clearing an earlier
  `CHANGES_REQUESTED` review.
- Complete: Treat a later `DISMISSED` review from the same reviewer as
  clearing an earlier `CHANGES_REQUESTED` review for `blocking_reviews`.
- Complete: Preserve `APPROVED` clearing behavior and non-counting/viewer-owned
  review exclusions.
- Complete: Stage and commit only the files changed for this thread.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `plans/dismissed_blocking_reviews_PLAN.md`
- `plans/dismissed_blocking_reviews_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_dismissed_review_clears_blocking_review -q
```

Result before implementation: failed because the earlier
`CHANGES_REQUESTED` review still had `blocks_merge=True` after a later
`DISMISSED` review from the same reviewer.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_dismissed_review_clears_blocking_review -q
```

Result after implementation: `1 passed in 0.67s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_commented_review_does_not_clear_blocking_review tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_approval_from_same_reviewer_clears_blocking_review -q
```

Result: `2 passed in 0.67s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```

Result: `114 passed in 3.09s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Result: `All checks passed!`

## Gaps

No planned requirement is partial or missing.
