# PRRT_kwDOSJAM6s6F6mWa Drop Stale Done Markers Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F6mWa_DROP_STALE_DONE_MARKERS_PLAN.md`

## Requirement Status

- Complete: Added a regression showing `_persist_state` removes stale
  initial-review and reviewer-settle done markers when the DB has matching
  re-armed started markers and no done markers.
- Complete: Concurrent DB started markers remain preserved during stale state
  persistence.
- Complete: Unrelated monitor thread state remains intact.
- Complete: Ran focused validation only; full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/lifecycle.py` so stale done-marker
  cleanup runs even when the DB started marker is already considered preserved.
- Added
  `test_persist_state_drops_stale_done_marker_when_freeze_started_matches` in
  `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k stale_done_marker_when_freeze_started_matches`
  failed because `__awf_initial_review_grace_done__:42` remained persisted.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "stale_done_marker_when_freeze_started_matches or preserves_concurrent_operator_hint_and_freeze"`
  passed with 2 tests.
- Focused style check:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.
