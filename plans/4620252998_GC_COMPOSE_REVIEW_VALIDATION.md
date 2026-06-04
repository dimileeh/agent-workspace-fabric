# Review Comment 4620252998 GC Compose Validation

Plan reference: `plans/4620252998_GC_COMPOSE_REVIEW_PLAN.md`

## Requirement Status

- Complete: Preserved-workspace compose teardown fallback now uses one clear
  status/reason extension table:
  `src/awf/service/gc.py::_PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES`.
- Complete: Lifecycle compose teardown tracking records a
  `COMPOSE_TEARDOWN_CALLBACK_RAISED` failed result before re-raising callback
  exceptions.
- Complete: Auth-overlay unmount remains gated on an observed tracked
  successful teardown.
- Complete: Focused regression coverage was added for the extension table and
  callback-raised tracking behavior.
- Complete: Broad AWF/GitHub validation was not run in the agent phase; AWF owns
  full validation, provenance, and merge gating after completion.

## Evidence

Changed files:

- `src/awf/service/gc.py`
- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`

Initial failing checks before production changes:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "preserved_compose_teardown_fallback_table_includes_completed_retention_state"`
  failed because `_PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES` did not exist.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "callback_raised_when_gc_raises_after_teardown"`
  failed because no tracked `monitor.compose_teardown_failed` result was logged.

Final focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "preserved_compose_teardown_fallback_table_includes_completed_retention_state or single_workspace_gc_tears_down_compose_for_retained_merged_workspace or single_workspace_gc_failed_within_retention_skips_fallback_compose_teardown"`
  passed: 3 passed, 35 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "callback_raised_when_gc_raises_after_teardown or raises_after_teardown"`
  passed: 2 passed, 23 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/service/test_gc_parts/test_gc_part_001.py tests/unit/runtime/test_monitor_completion_gc.py`
  passed.

## Gaps

None for the scoped review comment. Full repository validation and coverage are
managed by AWF/GitHub after the agent phase.
