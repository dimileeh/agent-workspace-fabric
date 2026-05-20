# Review Comment 4326668490 Validation

Plan reference: `REVIEW_COMMENT_4326668490_PLAN.md`

## Requirement Status

- Verify the `github_client.py` open-PR item URL parsing concern: Complete.
  Current code already validates `payload["url"]` is a string before assigning
  it, with regression coverage in `TestListOpenPullRequestsForBranch`.
- Add warning for invalid open-PR repo URL fallback: Complete.
  `BranchOpenPullRequestResolver.resolve` now logs
  `github.open_pr_lookup_skipped_invalid_repo_url` with redacted repo URL and
  error details before returning `[]`.
- Verify worker-restart executor recovery claim exclusivity: Complete.
  Current code already routes recovery adoption through
  `WorkspaceRepository.claim_worker_restart_recovery_execution`, which performs
  a conditional `UPDATE ... RETURNING` guarded by claim ownership/expiry.
- Verify preserved-active git subprocess timeout: Complete.
  Current code already passes `_PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS` to
  `subprocess.run` and converts `TimeoutExpired` into a failed result.
- Verify active-execution salvage duplicate-event race concern: Complete.
  Current code already locks the workspace with `get_for_update` before checking
  and recording the operator-required salvage event.
- Isolate `BranchOpenPullRequestResolver` in the affected service runtime unit
  test: Complete. The reviewed test now monkeypatches it to `_AnyInit` and
  asserts the worker receives the double.
- Add or update regression coverage for behavior changes: Complete.
  Added a resolver warning regression test and strengthened the service runtime
  isolation assertion.
- Do not push, switch branches, weaken existing assertions, or edit unrelated
  files: Complete.

## Evidence

Changed files:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/service/test_worker.py`
- `plans/REVIEW_COMMENT_4326668490_PLAN.md`
- `plans/REVIEW_COMMENT_4326668490_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestBranchOpenPullRequestResolver::test_invalid_repo_url_returns_empty_list_and_warns tests/unit/service/test_worker.py::test_build_worker_runtime_uses_local_service_node_id_instead_of_container_hostname -q`
  - First run failed before implementation, confirming both regression
    expectations.
  - Final run passed: `2 passed in 1.44s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py tests/unit/service/test_worker.py -q`
  - Passed: `119 passed in 3.00s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py tests/unit/service/test_worker.py`
  - Passed: `All checks passed!`.

## Gaps

No remaining planned gaps.
