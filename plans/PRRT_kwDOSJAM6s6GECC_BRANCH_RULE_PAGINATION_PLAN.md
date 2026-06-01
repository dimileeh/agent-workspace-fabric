# PRRT_kwDOSJAM6s6GECC Branch Rule Pagination Plan

## Problem Statement and Scope

An unresolved PR review thread reports that
`GitHubClient.fetch_branch_pull_request_allowed_merge_methods` reads only the
first page of `repos/{owner}/{repo}/rules/branches/{branch}`. GitHub paginates
that endpoint with a default page size of 30, so a later-page
`pull_request.allowed_merge_methods` rule can be missed and AWF can choose a
merge method disallowed by the base branch.

Scope is limited to branch ruleset fetching/parsing in
`src/awf/common/github_client.py` and focused unit coverage in
`tests/unit/common/test_github_client_parts/test_github_client_part_004.py`.

## Requirements Checklist

- Fetch all branch ruleset pages before deriving base-branch merge-method
  constraints.
- Preserve existing semantics for unconstrained rules, unknown-only method
  lists, multiple recognized rules, and error handling.
- Add a regression proving later-page `pull_request.allowed_merge_methods`
  rules are considered.
- Run only focused tests for the changed behavior; full AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Add a focused failing unit test that simulates `gh api --paginate --slurp`
   output where the first page has no relevant rule and the second page
   constrains merge methods.
2. Update the branch ruleset API invocation to request paginated output.
3. Normalize slurped paginated page arrays into the existing flat rule list
   before applying current merge-method parsing.
4. Run the focused GitHub client unit tests for `allowed_merge_methods`.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "allowed_merge_methods"
```

Pass criteria: the focused test selection passes and the new regression fails
before implementation when practical.
