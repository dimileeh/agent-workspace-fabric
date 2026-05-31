# Operator Hint Policy Block Validation

Plan reference: `plans/OPERATOR_HINT_POLICY_BLOCK_PLAN.md`

## Requirement Status

- Add a regression test that reproduces a policy-blocked operator-hint repair:
  Complete.
- Transition the pending operator hint to a terminal human-facing status when
  `_MonitorPolicyBlockedError` is raised during the operator hint cycle:
  Complete.
- Preserve the policy block reason in the hint status reason and returned
  result stderr: Complete.
- Avoid changing unrelated review-thread, push, or protected-scope behavior:
  Complete.
- Run only targeted checks for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`

Checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k policy_block`
  - Failed before implementation because the policy-block path returned
    `failed=True` and left the hint pending.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passed: 16 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  - Passed.

Full AWF/GitHub validation was not run in this agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
