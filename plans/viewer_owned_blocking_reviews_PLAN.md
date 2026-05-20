# Viewer-Owned Blocking Reviews Plan

## Problem Statement And Scope

An unresolved PR review thread reports that viewer-authored `CHANGES_REQUESTED`
reviews can appear in `PRStatus.blocking_reviews` even though viewer-authored
review bodies are filtered out of `unresolved_review_comments`. That can leave
the PR monitor in `NotifyHuman` with no agent-visible feedback to address.

Scope is limited to GitHub PR status parsing for effective review-state blockers.

## Requirements Checklist

- Add a regression test proving a viewer-authored `CHANGES_REQUESTED` review is
  ignored for both agent feedback and merge-blocking review state.
- Preserve existing behavior for non-viewer `CHANGES_REQUESTED` reviews,
  including empty-body blockers and later approval clearing.
- Keep non-counting reviewer filtering intact.
- Do not change GitHub write behavior, branch management, or PR comment handling.

## Implementation Steps

1. Add a focused unit test in `tests/unit/common/test_github_client.py`.
2. Run the new test and confirm it fails before the production change.
3. Update `_effective_blocking_reviews` to skip viewer-authored reviews.
4. Re-run the targeted GitHub client tests.
5. Run narrow validation commands appropriate for the changed Python area.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if practical in this workspace.
