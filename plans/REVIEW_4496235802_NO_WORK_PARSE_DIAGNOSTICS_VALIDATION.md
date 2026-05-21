# REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS Validation

Plan reference:
`plans/REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS_PLAN.md`

## Requirement Status

- Complete: Added a regression proving an unknown future `no_work`
  classification reason waits for preservation grace instead of creating a
  replacement.
- Complete: Updated preserved-active recovery so every non-expired `no_work`
  classification records blocked salvage and waits for grace.
- Complete: Added a regression proving multi-item all-fail open-PR parsing
  exposes aggregate failure context in the exception detail and batch warning.
- Complete: Preserved partial-success and single-item failure behavior for
  open-PR parsing while adding aggregate context only where later failures would
  otherwise be discarded.
- Complete: Preserved stale-active closed-connection behavior by checking the
  candidate through the database before runtime inspection.
- Complete: Preserved validation-requested/no-executor behavior by releasing
  the recovery transaction before recording blocked salvage.
- Complete: Focused unit, lint, and type validation passed.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/common/github_client.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/common/test_github_client.py`
- `plans/REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS_PLAN.md`
- `plans/REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_unknown_no_work_reason_waits_for_preservation_grace -q`
  - Before implementation: failed because recovery created a replacement for the
    unknown `no_work` reason during grace.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_all_malformed_pr_list_items_raise_aggregated_parse_context -q`
  - Before implementation: failed because the raised error had no aggregate
    parse failure context.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_scan_closed_connection_does_not_terminal_fail_workspace -q`
  - Before worker session fix: failed because runtime inspection still ran after
    the transient DB failure.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_salvage_without_executor_blocks_stale_cleanup -q`
  - Before worker session fix: timed out on the workspace row lock.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_unknown_no_work_reason_waits_for_preservation_grace tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_creates_one_replacement_with_lineage tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_cancels_superseded_active_operation tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_preserves_sync_remote_push_branch -q`
  - Result: 9 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_all_malformed_pr_list_items_raise_aggregated_parse_context tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_invalid_pr_list_head_repository_slug_is_invalid tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_missing_head_repository_identity_is_invalid tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_non_string_url_is_invalid -q`
  - Result: 4 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q`
  - Result: 139 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/common/test_github_client.py -q`
  - Result: 418 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/common/github_client.py tests/unit/control/test_worker.py tests/unit/common/test_github_client.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py src/awf/common/github_client.py`
  - Result: passed.

## Gaps

None.
