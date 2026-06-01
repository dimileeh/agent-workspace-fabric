# Review 4590903660 Slurp Empty Stdout Plan

## Problem Statement and Scope

Address the remaining actionable concerns embedded in review-level comment
`issue:4590903660` for PR #353:

- the merge-method rejection classifier relies on redacted `GitHubClientError.stderr`;
- `gh api --paginate --slurp` branch-rule calls returning empty stdout should not be treated as
  unconstrained branch policy.

Scope is limited to focused merge-method tests and the branch-rules GitHub client path.

## Requirements Checklist

- Add a regression that `redact_audit_text` preserves the GitHub merge-method policy phrases used by
  `_merge_method_rejection_method`.
- Add a regression that `fetch_branch_pull_request_allowed_merge_methods` raises
  `GitHubClientError` when `gh api --paginate --slurp` exits 0 with empty stdout.
- Preserve `_gh_json`'s existing empty-stdout behavior for callers where empty output still means
  "no data".
- Run only targeted validation; AWF/GitHub owns broad validation after agent completion.

## Implementation Steps

1. Add failing regressions in the existing merge-method and GitHub client test files.
2. Update the branch-rules fetch path to treat `payload is None` as an anomalous `--slurp` response
   and raise a diagnostic `GitHubClientError`.
3. Run focused pytest for the touched tests and focused ruff for touched files.
4. Record validation evidence in `plans/REVIEW_4590903660_SLURP_EMPTY_STDOUT_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q -k "redaction_preserves_merge_method_policy_phrases"`
  - Passes with no failures.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout"`
  - Passes with no failures.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  - Passes with no failures.

Full repository validation, coverage gates, and CI-equivalent checks are not run in the agent phase
per the AWF workspace contract.
