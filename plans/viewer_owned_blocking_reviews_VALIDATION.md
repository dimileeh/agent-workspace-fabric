# Viewer-Owned Blocking Reviews Validation

Plan reference: `viewer_owned_blocking_reviews_PLAN.md`

## Requirement Status

- Add a regression test proving a viewer-authored `CHANGES_REQUESTED` review is
  ignored for both agent feedback and merge-blocking review state: Complete.
- Preserve existing behavior for non-viewer `CHANGES_REQUESTED` reviews,
  including empty-body blockers and later approval clearing: Complete.
- Keep non-counting reviewer filtering intact: Complete.
- Do not change GitHub write behavior, branch management, or PR comment
  handling: Complete.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `plans/viewer_owned_blocking_reviews_PLAN.md`
- `plans/viewer_owned_blocking_reviews_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_viewer_owned_changes_requested_review_is_not_blocking -q`
  failed before the production change because `blocking_reviews` contained the
  viewer-authored review.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestFetchPrStatus::test_viewer_owned_changes_requested_review_is_not_blocking -q`
  passed after the production change.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q`
  passed with 114 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

No gaps remain.
