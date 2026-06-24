# Merge Rejection Origin Restamp Validation

Plan reference: `plans/MERGE_REJECTION_ORIGIN_RESTAMP_PLAN.md`

## Requirement Status

- Verify the review claim against local code: Complete. `MonitorState.merge_block_attention_active(...)`
  clears stale markers via `clear_merge_block_attention()`, which also removes
  the structured origin before the preserve re-stamp branch.
- Add focused regression coverage: Complete. The existing critical-section
  merge-rejection preservation test now asserts the origin survives in memory and
  in `monitor_threads_addressed`.
- Change only the required re-stamp behavior: Complete. The preserve branch now
  passes `originated_from_merge_rejection=True` when the marker was already known
  or proven to come from a merge rejection.
- Run focused checks only: Complete. Full AWF/GitHub validation is managed after
  agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/merge_attention.py`.
- Changed `tests/unit/runtime/test_pr_monitor_merge_attention.py`.
- Added this validation note and the corresponding plan.

Focused commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_status_preserves_stale_merge_rejection_attention_at_critical_section -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py
```

Results: all focused checks passed after implementation. The single regression
failed before the production change with a missing
`__awf_merge_block_attention_origin__` key, confirming the reviewer-reported bug.
