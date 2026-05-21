# PRRT_kwDOSJAM6s6DvQ9p Plan

## Problem Statement and Scope

The branch open-PR resolver currently converts an unparseable `repo_url` into
`[]`. Preserved-active recovery treats `[]` as a true no-match result, which can
allow no-PR fallback handling even though lookup was never attempted.

Scope is limited to surfacing invalid repository URLs from
`BranchOpenPullRequestResolver.resolve` as a structured lookup failure while
preserving redacted logging and existing valid/no-match behavior.

## Requirements Checklist

- Invalid `repo_url` in branch open-PR resolution must not return an empty list.
- The failure must be structured so callers that already handle resolver
  exceptions classify the lookup as failed.
- Logs and exception details must not leak credentials embedded in repository
  URLs.
- Valid repository URLs with no open PRs must still return `[]`.
- Add or update focused regression coverage.

## Implementation Steps

1. Update the existing resolver test to expect a `PullRequestMetadataError`
   for invalid repository URLs and confirm it fails before implementation.
2. Change `BranchOpenPullRequestResolver.resolve` to raise a structured
   open-PR lookup error after logging the redacted invalid URL.
3. Run the focused resolver tests.
4. Run targeted lint or broader validation if the touched surface requires it.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestBranchOpenPullRequestResolver -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  must pass.
