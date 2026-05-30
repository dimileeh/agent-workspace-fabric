# Operator Hint Protected-Scope Validation

Plan reference: `plans/OPERATOR_HINT_PROTECTED_SCOPE_PLAN.md`

## Requirement Status

- Complete: Catch `ProtectedScopeDiffError` from the operator-hint
  CLI/dirty-commit path.
- Complete: Return the existing protected-scope diff-unavailable push result
  for the workspace and remote branch.
- Complete: Preserve existing policy-block, ownership-failure, verdict, and
  successful-push handling.
- Complete: Add a regression test for operator-hint repair receiving
  `ProtectedScopeDiffError`.
- Complete: Avoid broad AWF/GitHub-owned validation.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/OPERATOR_HINT_PROTECTED_SCOPE_PLAN.md`
- `plans/OPERATOR_HINT_PROTECTED_SCOPE_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result -q`
  - First run failed before implementation with uncaught
    `ProtectedScopeDiffError`.
  - Second run passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passed: 3 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passed.

Full AWF/GitHub-owned validation was not run inside this agent phase; AWF owns
the broad validation, provenance, logs, timeouts, and merge gating after
completion.
