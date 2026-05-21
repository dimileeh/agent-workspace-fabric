# Review 4496235802 Branch PR Lookup Validation

Plan reference: `plans/REVIEW_4496235802_BRANCH_PR_LOOKUP_PLAN.md`

## Requirement Status

- Complete: Mixed parseable/malformed PR list responses return parseable
  matches while logging warnings for malformed items.
  - Evidence: `src/awf/common/github_client.py` no longer raises when at
    least one PR item parses successfully.
  - Evidence:
    `test_mixed_malformed_and_parseable_items_returns_parseable_matches`
    asserts the parseable PR is returned and the malformed sibling is warned.

- Complete: All-malformed PR list responses continue to fail closed with
  `OPEN_PR_LOOKUP_INVALID`.
  - Evidence: the existing `ListOpenPullRequestsForBranch` test group passed
    after the implementation change.

- Complete: Duplicate/ambiguity logic remains based on parseable matches.
  - Evidence: branch PR lookup tests passed without changing resolver or worker
    ambiguity handling.

- Complete: `get_worktree_path` returning `None` remains a retryable failed
  preserved-active worktree classification during preservation grace.
  - Evidence: current `src/awf/control/worker.py` maps a missing worktree root
    to `state="failed"`, and the targeted worker regression tests passed.

- Complete: Regression test was updated before production code and failed for
  the current mixed-results raise.

- Complete: Narrow tests and lint passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q -k 'mixed_malformed_and_parseable_items'`
  - Before implementation: failed with `OPEN_PR_LOOKUP_INVALID` from the
    mixed-results raise.
  - After implementation: passed, `1 passed, 137 deselected`.

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q -k 'ListOpenPullRequestsForBranch'`
  - Passed, `16 passed, 122 deselected`.

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_unavailable_worktree_root_classifies_as_failed or preserved_active_unknown_worktree_root_retries_during_grace or preserved_active_unknown_worktree_root_requires_operator_recovery_after_grace'`
  - Passed, `3 passed, 266 deselected`.

- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  - Passed.

## Gaps

None.
