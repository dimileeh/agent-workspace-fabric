# Operator Hint Persist Race Validation

Plan reference: `plans/operator_hint_persist_race_PLAN.md`

## Requirement Status

- Preserve a concurrently persisted DB operator hint when in-memory state did
  not know about it: Complete.
- Preserve remonitor freeze markers and avoid restoring stale elapsed markers:
  Complete.
- Do not resurrect a hint that the current state has already processed:
  Complete.
- Keep unrelated addressed-thread and sync-base persistence intact: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/operator_hint_persist_race_PLAN.md`
- `plans/operator_hint_persist_race_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_persist_state_preserves_concurrent_operator_hint_and_freeze -q`
  - Failed before the fix with missing `__awf_pending_operator_hint__`.
  - Passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passed: `6 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::test_sync_base_no_progress_state_is_persisted_across_restarts -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/lifecycle.py`
  - Passed.

Full AWF/GitHub-owned validation was not run in the agent phase, per the AWF
workspace contract. AWF/GitHub CI owns broad validation, provenance, logs, and
merge gating after agent completion.

## Gaps

None.
