# Review 4496235802 Branch PR Lookup Plan

## Problem Statement And Scope

Address the current review-level feedback for PR `#272` comment
`issue:4496235802`.

Scope is limited to:

- `src/awf/common/github_client.py` branch open-PR lookup handling when `gh pr
  list` returns a mix of parseable PRs and malformed sibling items.
- Focused regression coverage in `tests/unit/common/test_github_client.py`.
- Verifying the worker-side `worktree_root_unavailable` concern is already
  handled by the current code and tests.

## Requirements Checklist

- [ ] Mixed parseable/malformed PR list responses must return parseable matches
  while logging warnings for malformed items.
- [ ] All-malformed PR list responses must continue to fail closed with
  `OPEN_PR_LOOKUP_INVALID`.
- [ ] Existing duplicate/ambiguity logic must continue to operate on the
  parseable matches returned by the lookup.
- [ ] `get_worktree_path` returning `None` must remain a retryable failed
  preserved-active worktree classification during preservation grace.
- [ ] Add or update regression tests before the production code change and
  confirm the relevant test fails before implementation when practical.
- [ ] Run narrow tests and lint for touched Python files.

## Implementation Steps

1. Update the mixed malformed/parseable PR-list test to expect parseable
   results plus a warning instead of `OPEN_PR_LOOKUP_INVALID`.
2. Run that targeted test to confirm it fails against the current
   fail-closed implementation.
3. Remove the mixed-results raise from `list_open_pull_requests_for_branch`
   while preserving warnings and the all-malformed failure.
4. Run the targeted GitHub client tests plus the worker regression tests that
   cover `worktree_root_unavailable` retry/operator behavior.
5. Run ruff on changed Python files and record validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q -k 'mixed_malformed_and_parseable_items'`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q -k 'ListOpenPullRequestsForBranch'`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_unavailable_worktree_root_classifies_as_failed or preserved_active_unknown_worktree_root_retries_during_grace or preserved_active_unknown_worktree_root_requires_operator_recovery_after_grace'`
  - Passes, showing the worker-side review concern is already covered.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  - No lint errors.
