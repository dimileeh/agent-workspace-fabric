# PRRT_kwDOSJAM6s6DdvJK Plan

## Problem Statement And Scope

The preserved-active-execution salvage path can attach a PR monitor from a
single `gh pr list --head <branch>` result without verifying that the PR head
repository is the workspace repository. In fork-heavy repositories, branch-name
matches are not sufficient to identify the intended PR.

Scope is limited to branch open-PR lookup metadata, preserved active execution
salvage matching, and focused regression tests for review thread
`PRRT_kwDOSJAM6s6DdvJK`.

## Requirements Checklist

- Request `headRepository` / `headRepositoryOwner` from `gh pr list`.
- Parse and carry the PR head repository identity in branch lookup results.
- Reject or mark operator-required when a single branch match is from a
  different head repository than the workspace repository.
- Preserve the existing valid same-repository single-match salvage behavior.
- Add regression tests for parser field coverage and mismatched-fork ambiguity.

## Implementation Steps

1. Extend `BranchOpenPullRequest` with `head_repo_slug`.
2. Include `headRepository` and `headRepositoryOwner` in
   `_BRANCH_OPEN_PR_LIST_JSON_FIELDS`.
3. Parse a required head repository slug for open branch PR list items.
4. Carry `head_repo_slug` through `_OpenPullRequestSummary` payloads.
5. In preserved active branch PR resolution, compare summaries against the
   workspace repo slug and mark non-matching results ambiguous.
6. Update and add tests for the new parser and worker behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "pushed_branch_pr" -q`

Pass criteria: both commands pass and the validation file records requirement
coverage.
