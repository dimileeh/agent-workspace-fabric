# PRRT_kwDOSJAM6s6F6t_p Preserve Elapsed Settle Markers Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F6t_p_PRESERVE_ELAPSED_SETTLE_PLAN.md`

## Requirement Status

- Complete: Added a regression showing `_persist_state` preserves a
  reviewer-settle done marker newly marked elapsed from the matching persisted
  started marker.
- Complete: Existing stale done-marker cleanup remains covered by the adjacent
  stale freeze regression.
- Complete: A concurrent re-arm with a different DB started marker still drops
  the current state's newly elapsed done marker.
- Complete: The merge remains generic for wait markers by tracking keys written
  through `MonitorState.mark_addressed` and only preserving DB-missing done
  markers when they were newly marked in the current state.
- Complete: Dirty marker tracking is cleared after successful persistence so a
  later stale in-memory done marker is not treated as newly elapsed.
- Complete: Ran focused validation only; full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

- Added
  `test_persist_state_preserves_newly_elapsed_settle_done_marker` and
  `test_persist_state_drops_newly_elapsed_settle_done_after_concurrent_rearm`
  in `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
- Changed `MonitorState.mark_addressed` in `src/awf/runtime/pr_monitor.py` to
  track changed thread keys for the current in-memory monitor state.
- Changed `src/awf/runtime/pr_monitor_runner/lifecycle.py` so concurrent
  freeze-state merging keeps a DB-missing done key only when the current pass
  newly marked that key.
- Red:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k newly_elapsed_settle_done_marker`
  failed because `__awf_non_check_reviewer_settle_done__:42:ffffffffffffffffffffffffffffffffffffffff`
  was missing after persistence.
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "newly_elapsed_settle_done or stale_done_marker_when_freeze_started_matches or preserves_concurrent_operator_hint_and_freeze"`
  passed with 4 tests.
- Focused style:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.
- Focused format:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.
