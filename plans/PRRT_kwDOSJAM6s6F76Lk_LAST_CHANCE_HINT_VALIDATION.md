# PRRT_kwDOSJAM6s6F76Lk Last-Chance Hint Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F76Lk_LAST_CHANCE_HINT_PLAN.md`

## Requirement Status

- Complete: Added `test_merge_last_chance_recheck_blocks_hint_written_after_final_refresh` to persist an operator hint immediately after the existing final refresh and assert that `gh pr merge` is not called.
- Complete: Added a last-chance operator-state refresh in `src/awf/runtime/pr_monitor_runner/merge_loop.py` before the merge operation begins.
- Complete: The last-chance refresh reuses initial-review-grace and non-check-reviewer settle rechecks when refreshed operator state keeps the action as `Merge`.
- Complete: Validation stayed focused on the touched runtime/test behavior. Full AWF/GitHub validation is intentionally left to AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py`
- `plans/PRRT_kwDOSJAM6s6F76Lk_LAST_CHANCE_HINT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F76Lk_LAST_CHANCE_HINT_VALIDATION.md`

Commands run:

- Before implementation: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py::test_merge_last_chance_recheck_blocks_hint_written_after_final_refresh -q`
  - Result: failed because the monitor returned terminal `True`, showing it merged instead of handling the persisted hint.
- After implementation: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py::test_merge_last_chance_recheck_blocks_hint_written_after_final_refresh -q`
  - Result: passed.
- After implementation: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py -q`
  - Result: `11 passed`.
- After implementation: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py`
  - Result: passed.

## Gaps

No planned requirement is partial or missing.
