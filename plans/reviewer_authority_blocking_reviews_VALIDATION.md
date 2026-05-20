# Reviewer Authority Blocking Reviews Validation

Plan reference: `plans/reviewer_authority_blocking_reviews_PLAN.md`

## Requirement Status

- Complete: Fetch reviewer authority for pull request reviews.
- Complete: Preserve non-counting review bodies in
  `unresolved_review_comments`.
- Complete: Do not include explicit non-counting `CHANGES_REQUESTED` reviews in
  `blocking_reviews`.
- Complete: Preserve existing effective-review behavior for counting reviewers,
  including later approvals clearing earlier change requests.
- Complete: Prefer conservative behavior when authority data is absent from
  older/fake payloads.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `plans/reviewer_authority_blocking_reviews_PLAN.md`
- `plans/reviewer_authority_blocking_reviews_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_non_counting_changes_requested_review_is_advisory_not_blocking -q
```

Result before implementation: failed because the non-counting change request
still set `blocks_merge=True`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_non_counting_changes_requested_review_is_advisory_not_blocking -q
```

Result after implementation: `1 passed in 0.62s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```

Result: `113 passed in 2.79s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 157 source files`

## Gaps

No planned requirement is partial or missing.
