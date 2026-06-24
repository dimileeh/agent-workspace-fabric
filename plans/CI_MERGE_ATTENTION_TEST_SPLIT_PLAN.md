# CI Merge Attention Test Split Plan

## Problem Statement And Scope

PR #678 fails CI in two visible Python coverage shards:

- `python-coverage-shards (8)` fails because
  `tests/unit/runtime/test_pr_monitor_merge_attention.py` has grown to 1,618
  lines, exceeding the repository maintainability guard's 1,500-line maximum
  for first-party files.
- `python-coverage-shards (5)` fails because stale branch-protection attention
  remains set when GitHub reports a clean mergeability status before the monitor
  parks on merge-queue or reviewer-settle waits.

Scope is limited to merge-attention cleanup in
`src/awf/runtime/pr_monitor_runner/merge_attention.py`, focused tests under
`tests/**`, and the plan/validation documents.

## Requirements Checklist

- [ ] Keep all first-party files at or below the 1,500-line maintainability
      guard.
- [ ] Preserve the existing merge-attention regression coverage and assertions.
- [ ] Clear stale, non-structured merge-block attention before clean GitHub
      merge-queue and reviewer-settle waits.
- [ ] Preserve structured merge-rejection-origin attention until an actual merge
      retry confirms resolution.
- [ ] Avoid broad AWF/GitHub-owned validation; run only focused local commands
      that prove the split and the affected tests.
- [ ] Commit the CI repair locally without pushing.

## Implementation Steps

1. Move the lower-level merge-attention persistence helper regressions from the
   oversized module into a focused sibling test module.
2. Keep shared fixtures/imports local to each module so tests remain readable
   and independent.
3. Reproduce the two shard-5 failures locally.
4. Make structured merge-rejection origin authoritative for queue/reviewer wait
   preservation, avoiding ambiguous reason-text preservation for stale generic
   branch-protection attention.
5. Run the maintainability guard and the affected merge-attention and
   merge-queue tests.
6. Record verification evidence in
   `plans/CI_MERGE_ATTENTION_TEST_SPLIT_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized files.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q`
  - Passes, preserving the split test coverage.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_reviewer_settle_wait_when_forge_resolved -q`
  - Passes, proving stale clean-status attention is cleared before non-human
    waits.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_status_preserves_merge_rejection_attention_during_queue_wait tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_structured_merge_rejection_preserve_uses_state_not_db -q`
  - Passes, proving structured merge-rejection-origin attention is still
    preserved.

Full AWF/GitHub validation remains managed by AWF after agent completion.
