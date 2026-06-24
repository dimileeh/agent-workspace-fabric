# Merge Rejection Origin Validation

Plan reference: `MERGE_REJECTION_ORIGIN_PLAN.md`

## Requirement Status

- Store merge-rejection origin as structured monitor state instead of relying on
  `awaiting_human_reason` text for new markers: Complete.
- Persist the origin marker atomically with the existing merge-block marker and
  attention row update: Complete.
- Clear the origin marker whenever the merge-block marker is cleared: Complete.
- Preserve compatibility for already-persisted legacy merge-rejection markers
  that only have the prior reason text: Complete, via a prefix-only fallback when
  no structured origin key exists.
- Add focused regression coverage proving a changed human-readable reason still
  preserves merge-rejection attention when the structured origin flag is set:
  Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor.py`
- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_state.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`

Test-first failure:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  failed before implementation with `TypeError` for the new
  `originated_from_merge_rejection` marker API.

Passing focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  passed: 32 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_state.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py`
  passed.

Full AWF/GitHub validation is managed after agent completion and was not run in
this repair cycle.
