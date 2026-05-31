# Remonitor Hint Terminal Race Validation

Plan reference: `plans/REMONITOR_HINT_TERMINAL_RACE_PLAN.md`

## Requirement Status

- Complete: Mark an operator hint `needs_human` before returning a terminal
  ownership repair failure from `_run_operator_hint_cycle`.
- Complete: Preserve terminal push-result semantics and the ownership-repair
  reason code.
- Complete: Re-read operator hint/freeze state immediately before `merge_pr()`
  when all other merge gates have passed.
- Complete: If the final re-read imports a pending hint, dispatch the hint
  repair instead of merging.
- Complete: If the final re-read imports freeze-only review grace or settle
  markers, wait instead of merging.
- Complete: Validation remained focused; broad AWF/GitHub-owned validation was
  not run during the agent phase.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`

Focused checks:

- Initial regression run:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  failed on the three new regressions before implementation.
- Final behavior run:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  passed with `27 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/merge_loop.py`
  passed.

Full AWF/GitHub validation is intentionally left to AWF after agent completion
per the workspace contract.
