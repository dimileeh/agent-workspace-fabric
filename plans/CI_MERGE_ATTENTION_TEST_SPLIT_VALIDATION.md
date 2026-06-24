# CI Merge Attention Test Split Validation

Plan reference: `plans/CI_MERGE_ATTENTION_TEST_SPLIT_PLAN.md`

## Requirement Status

- Complete: Keep all first-party files at or below the 1,500-line
  maintainability guard.
  - Evidence: `tests/unit/runtime/test_pr_monitor_merge_attention.py` is 998
    lines and `tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
    is 655 lines.
- Complete: Preserve the existing merge-attention regression coverage and
  assertions.
  - Evidence: the persistence-helper and atomic-clear regressions were moved
    unchanged into
    `tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`.
- Complete: Clear stale, non-structured merge-block attention before clean
  GitHub merge-queue and reviewer-settle waits.
  - Evidence: the two shard-5 failing tests were reproduced locally before the
    fix, then passed after making structured merge-rejection origin authoritative
    for preservation.
- Complete: Preserve structured merge-rejection-origin attention until an
  actual merge retry confirms resolution.
  - Evidence: focused structured-origin preservation tests passed after the
    change.
- Complete: Avoid broad AWF/GitHub-owned validation.
  - Evidence: only targeted ruff and pytest commands listed below were run.
    Full AWF/GitHub validation remains managed by AWF after agent completion.
- Complete: Commit the CI repair locally without pushing.
  - Evidence: local commit will be created after this validation document is
    written; no push is performed by the agent.

## Commands Run

- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `1 passed in 0.41s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q`
  - Passed before the behavior fix: `23 passed in 30.91s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_reviewer_settle_wait_when_forge_resolved -q`
  - Failed before the fix, matching CI shard 5: both tests left
    `workspace.awaiting_human_since` set.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py tests/unit/runtime/test_merge_queue_ordering.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_reviewer_settle_wait_when_forge_resolved -q`
  - Passed after the fix: `2 passed in 4.76s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_status_preserves_merge_rejection_attention_during_queue_wait tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_structured_merge_rejection_preserve_uses_state_not_db -q`
  - Passed after the fix: `2 passed in 3.07s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Re-run after the behavior fix; passed: `1 passed in 0.41s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q`
  - Re-run after the behavior fix; passed: `23 passed in 30.78s`.

## Files Changed

- `tests/unit/runtime/test_pr_monitor_merge_attention.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `plans/CI_MERGE_ATTENTION_TEST_SPLIT_PLAN.md`
- `plans/CI_MERGE_ATTENTION_TEST_SPLIT_VALIDATION.md`
