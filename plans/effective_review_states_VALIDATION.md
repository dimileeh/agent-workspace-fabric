# Effective Review States Validation

Plan reference: `plans/effective_review_states_PLAN.md`

## Requirement Status

- Complete: Preserve advisory review comments in `unresolved_review_comments`.
- Complete: Keep `CHANGES_REQUESTED` reviews blocking until the same reviewer
  submits a later `APPROVED` review.
- Complete: Ensure later `COMMENTED` reviews from the same reviewer do not
  clear an earlier blocking review.
- Complete: Ignore non-merge-gating review states such as `DISMISSED` while
  calculating effective blocking reviews.
- Complete: Keep existing bot advisory and empty-body review behavior unchanged.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `plans/effective_review_states_PLAN.md`
- `plans/effective_review_states_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_commented_review_does_not_clear_blocking_review -q
```

Result before implementation: failed because `blocks_merge` was `[False, False]`
instead of `[True, False]`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_commented_review_does_not_clear_blocking_review tests/unit/common/test_github_client.py::TestFetchPrStatus::test_later_dismissed_review_does_not_clear_blocking_review -q
```

Result: `2 passed in 0.68s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```

Result: `112 passed in 4.69s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Result: `All checks passed!`

## Gaps

No planned requirement is partial or missing.
