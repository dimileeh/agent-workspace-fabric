# Operator Hint Terminal Stale Persist Validation

Plan reference: `plans/operator_hint_terminal_stale_persist_PLAN.md`

## Requirement Status

- Complete: A stale pending hint with the same operation ID must not overwrite a
  DB terminal hint status.
- Complete: The persisted hint retains the DB terminal status reason.
- Complete: Existing processed-marker behavior keeps clearing already processed
  hints.
- Complete: Verification used focused commands only. Full AWF/GitHub validation
  is managed by AWF after agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/lifecycle.py` so concurrent
  operator hint merge preserves terminal DB hints for the same operation ID
  after honoring processed markers.
- Added
  `tests/unit/runtime/test_pr_monitor_operator_hints.py::test_persist_state_preserves_concurrent_terminal_operator_hint_status`
  for both `needs_human` and `agent_failed`.

## Commands

- Initial failing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_persist_state_preserves_concurrent_terminal_operator_hint_status -q`
  failed with persisted status `pending` instead of the DB terminal status.
- Passing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_persist_state_preserves_concurrent_terminal_operator_hint_status -q`
- Focused unit surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
