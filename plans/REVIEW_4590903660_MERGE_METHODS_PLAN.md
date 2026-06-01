# Review 4590903660 Merge Methods Plan

## Problem Statement And Scope

Address review-level feedback from PR comment `issue:4590903660` about PR monitor merge-method handling. Scope is limited to:

- branch ruleset parsing for explicit empty `allowed_merge_methods`;
- merge-method rejection rotation only when GitHub rejects the attempted method;
- redaction coverage for all merge-method policy classifier phrases.

## Requirements Checklist

- Add or update regression coverage before implementation.
- Treat an explicit empty `allowed_merge_methods: []` on a pull-request rule as a real empty constraint, not as unconstrained.
- Do not rotate to another merge method when the GitHub rejection names a different merge method than the method AWF attempted.
- Preserve policy phrase redaction coverage for all classifier phrase variants.
- Keep validation focused; broad AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Update focused unit tests for the three review findings.
2. Run the targeted tests to confirm the current implementation fails.
3. Implement the smallest production changes in `github_client.py` and `merge_loop.py`.
4. Re-run the same focused tests and any narrow syntax/lint check needed for touched files.
5. Record validation evidence in `plans/REVIEW_4590903660_MERGE_METHODS_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py::TestMutations::test_fetch_branch_pull_request_allowed_merge_methods_empty_unconstrained tests/unit/common/test_github_client_parts/test_github_client_part_004.py::TestMutations::test_fetch_branch_pull_request_allowed_merge_methods_omitted_methods_unconstrained tests/unit/common/test_github_client_parts/test_github_client_part_004.py::TestMutations::test_fetch_branch_pull_request_allowed_merge_methods_empty_list_is_empty_policy tests/unit/common/test_github_client_parts/test_github_client_part_004.py::TestMutations::test_fetch_branch_pull_request_allowed_merge_methods_unknown_only_unconstrained tests/unit/common/test_github_client_parts/test_github_client_part_004.py::TestMutations::test_fetch_branch_pull_request_allowed_merge_methods_intersects_multiple_rules -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`

Pass criteria: both focused unit test selections pass. Full repository validation and CI-equivalent gates are intentionally not run inside the agent phase.
