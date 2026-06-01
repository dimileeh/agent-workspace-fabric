# Review 4590903660 Slurp Empty Stdout Validation

Plan reference:
`REVIEW_4590903660_SLURP_EMPTY_STDOUT_PLAN.md`

## Requirement Status

- `redact_audit_text` preserves the GitHub merge-method policy phrases used by
  `_merge_method_rejection_method`: Complete.
- `fetch_branch_pull_request_allowed_merge_methods` raises `GitHubClientError` when
  `gh api --paginate --slurp` exits 0 with empty stdout: Complete.
- `_gh_json`'s existing empty-stdout behavior remains unchanged for other callers: Complete.
- Run only targeted validation; AWF/GitHub owns broad validation after agent completion:
  Complete.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `plans/REVIEW_4590903660_SLURP_EMPTY_STDOUT_PLAN.md`
- `plans/REVIEW_4590903660_SLURP_EMPTY_STDOUT_VALIDATION.md`

Initial failing check:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout"`
  failed before implementation because `fetch_branch_pull_request_allowed_merge_methods` returned
  normally instead of raising `GitHubClientError`.

Focused passing checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q -k "redaction_preserves_merge_method_policy_phrases"`
  passed with `1 passed, 19 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout"`
  passed with `1 passed, 48 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "fetch_branch_pull_request_allowed_merge_methods"`
  passed with `12 passed, 37 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passed.

Full repository validation, coverage gates, and CI-equivalent checks were not run in the agent phase
per the AWF workspace contract.
