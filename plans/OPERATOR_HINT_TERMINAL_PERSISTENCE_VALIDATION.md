# Operator Hint Terminal Persistence Validation

Plan reference: `plans/OPERATOR_HINT_TERMINAL_PERSISTENCE_PLAN.md`

## Requirement Status

- Add a regression test proving terminal operator-hint protected-scope failures
  persist the hint status as `needs_human`: Complete.
- Cover both terminal protected-scope reason codes
  `PROTECTED_SCOPE_PUSH_BLOCKED` and `PROTECTED_SCOPE_DIFF_UNAVAILABLE`:
  Complete.
- Persist monitor state before terminal failure handling in the
  `AddressOperatorHint` branch: Complete.
- Keep changes scoped to PR monitor runtime behavior, its tests, and plan
  documentation: Complete.
- Use focused validation only; AWF/GitHub own broad validation after this agent
  phase: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/loop.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/OPERATOR_HINT_TERMINAL_PERSISTENCE_PLAN.md`
- `plans/OPERATOR_HINT_TERMINAL_PERSISTENCE_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_terminal_failure_persists_needs_human_status -q`
  - Failed before the runtime change because the persisted hint remained
    `status: pending`.
  - Passed after the runtime change.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passed: 21 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passed.

Full AWF/GitHub validation was not run locally per the workspace contract; AWF
owns broad post-agent validation, provenance, and merge gating.
