# Operator Hint Pushed Persist Validation

Plan reference: `plans/operator_hint_pushed_persist_PLAN.md`

## Requirement Status

- Persist processed operator-hint state immediately when an operator-hint repair
  successfully pushes commits: Complete.
- Preserve the existing terminal/no-op persistence behavior: Complete.
- Add a regression test that fails without the pushed-path persistence:
  Complete.
- Do not run broad AWF/GitHub validation; use targeted tests only: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/loop.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/operator_hint_pushed_persist_PLAN.md`
- `plans/operator_hint_pushed_persist_VALIDATION.md`

Focused checks:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k pushed_processed_status_is_persisted_before_return`
  failed because `__awf_pending_operator_hint__` remained persisted after a
  successful pushed operator-hint repair.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k pushed_processed_status_is_persisted_before_return`
  passed.
- Adjacent persistence coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "pushed_processed_status_is_persisted_before_return or non_pushed_terminal_status_is_persisted_before_return or noop_processed_status_is_persisted_before_return"`
  passed.
- Touched-file lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.

Full AWF/GitHub validation was not run locally; AWF owns broad validation after
agent completion.

## Gaps

No gaps remain for the planned scope.
