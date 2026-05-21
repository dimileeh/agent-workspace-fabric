# Mixed Open PR Parse Plan

## Problem Statement And Scope

The branch open-PR lookup currently drops malformed `gh pr list` items when at least one item parses successfully. Preserved-active recovery can treat a single returned match as authoritative, so mixed parse success/failure must fail closed instead of returning a partial subset.

Scope is limited to `list_open_pull_requests_for_branch` behavior and its unit tests.

## Requirements Checklist

- Fail closed when `gh pr list` returns both parseable PR items and malformed PR items.
- Preserve existing behavior for all-malformed payloads: raise the parsing failure.
- Preserve existing behavior for fully parseable payloads.
- Keep failure detail useful enough to diagnose malformed item positions.

## Implementation Steps

1. Update the focused GitHub client unit test to expect a structured lookup error for mixed parseable/malformed results.
2. Confirm the updated regression fails against the current implementation.
3. Change `list_open_pull_requests_for_branch` so any item parse failure raises `PullRequestMetadataError` after logging malformed item context.
4. Run the narrow unit test file, then ruff on touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`

Pass criteria: both commands exit successfully.
