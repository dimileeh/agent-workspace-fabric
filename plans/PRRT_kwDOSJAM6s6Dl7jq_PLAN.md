# PRRT_kwDOSJAM6s6Dl7jq Plan

## Problem Statement and Scope

The open PR salvage path uses `list_open_pull_requests_for_branch` to find open
pull requests for a branch. That helper invokes `gh pr list --head ... --state
open` without an explicit fetch limit, so GitHub CLI returns only its default
30 items. In repositories with many fork PRs sharing a branch name, salvage
could make decisions from a truncated candidate set.

Scope is limited to the GitHub client branch-PR lookup command shape and its
unit coverage.

## Requirements Checklist

- Add a regression test proving branch PR lookup requests more than the `gh pr
  list` default page size.
- Keep existing parsing, error handling, and branch/base filtering behavior.
- Make the smallest code change needed so salvage lookup is not limited to the
  GitHub CLI default page.
- Run targeted validation for `tests/unit/common/test_github_client.py`.

## Implementation Steps

1. Add a failing unit test in `tests/unit/common/test_github_client.py` that
   asserts `list_open_pull_requests_for_branch` passes an explicit `--limit`
   greater than 30.
2. Update `src/awf/common/github_client.py` to include that limit in the `gh pr
   list` command.
3. Run the targeted unit test file.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6Dl7jq_VALIDATION.md`.
